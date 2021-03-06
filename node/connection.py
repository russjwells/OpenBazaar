import errno
import json
import logging
import platform
from pprint import pformat
from urlparse import urlparse

import obelisk
import zmq
from zmq.error import ZMQError
from zmq.eventloop import ioloop, zmqstream

from node import constants, network_util
from node.crypto_util import Cryptor
from node.guid import GUIDMixin


class PeerConnection(object):
    def __init__(self, transport, address, nickname=""):

        self.transport = transport
        self.address = address
        self.nickname = nickname

        # Establishing a ZeroMQ stream object
        self.ctx = transport.ctx
        self.socket = self.ctx.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.RECONNECT_IVL, 2000)
        self.socket.setsockopt(zmq.RECONNECT_IVL_MAX, 16000)
        self.stream = zmqstream.ZMQStream(
            self.socket, io_loop=ioloop.IOLoop.current())

        self.log = logging.getLogger(
            '[%s] %s' % (self.transport.market_id, self.__class__.__name__)
        )

        self._initiate_connection()

    def _initiate_connection(self):
        try:
            self.socket.connect(self.address)
        except zmq.ZMQError as err:
            if err.errno != errno.EINVAL:
                raise
            self.socket.ipv6 = True
            try:
                self.socket.connect(self.address)
            except zmq.ZMQError as err:
                self.log.error('Bad URI %s', err)

    def send(self, data, callback):
        self.send_raw(json.dumps(data), callback)

    def send_raw(self, serialized, callback=None):
        self.stream.send(serialized)

        def cb(_, msg):
            try:
                response = json.loads(msg[0])
            except ValueError:
                self.log.error('[send_raw] Bad JSON response: %s', msg[0])
                return
            self.log.datadump('[send_raw] response: %s', pformat(response))

            # Update active peer info
            self.nickname = response.get('senderNick', self.nickname)
            if callback is not None:
                self.log.debug('%s', msg)
                callback(msg)

        self.stream.on_recv_stream(cb)


class CryptoPeerConnection(GUIDMixin, PeerConnection):

    def __init__(self, transport, address, pub=None, guid=None, nickname="",
                 sin=None):

        GUIDMixin.__init__(self, guid)
        PeerConnection.__init__(self, transport, address, nickname)

        self.pub = pub

        # Convert URI over
        url = urlparse(address)
        self.ip = url.hostname
        self.port = url.port

        self.sin = sin
        self.address = "tcp://%s:%s" % (self.ip, self.port)

    def start_handshake(self, initial_handshake_cb=None):
        def cb(msg, handshake_cb=None):
            if not msg:
                return

            self.log.debugv('ALIVE PEER %s', msg[0])
            msg = msg[0]
            try:
                msg = json.loads(msg)
            except ValueError:
                self.log.error('[start_handshake] Bad JSON response: %s', msg)
                return

            # Update Information
            self.guid = msg['senderGUID']
            self.sin = self.generate_sin(self.guid)
            self.pub = msg['pubkey']
            self.nickname = msg['senderNick']

            # Add this peer to active peers list
            self.transport.dht.add_as_active_peer(self)

            if initial_handshake_cb is not None:
                initial_handshake_cb()

        self.send_raw(
            json.dumps({
                'type': 'hello',
                'pubkey': self.transport.pubkey,
                'uri': self.transport.uri,
                'senderGUID': self.transport.guid,
                'senderNick': self.transport.nickname,
                'senderNamecoin': self.transport.namecoin_id,
                'v': constants.VERSION
            }),
            cb
        )

    def __repr__(self):
        return '{ guid: %s, ip: %s, port: %s, pubkey: %s }' % (
            self.guid, self.ip, self.port, self.pub
        )

    @staticmethod
    def generate_sin(guid):
        return obelisk.EncodeBase58Check('\x0F\x02%s' + guid.decode('hex'))

    def sign(self, data):
        cryptor = Cryptor(privkey_hex=self.transport.settings['secret'])
        return cryptor.sign(data)

    def encrypt(self, data):
        """
        Encrypt the data with self.pub and return the ciphertext.
        @raises Exception: The encryption failed.
        """
        assert self.pub, "Attempt to encrypt without key."
        cryptor = Cryptor(pubkey_hex=self.pub)
        return cryptor.encrypt(data)

    def send(self, data, callback=None):
        assert self.guid, 'Uninitialized own guid'

        if not self.pub:
            self.log.warn('There is no public key for encryption')
            return

        # Include sender information and version
        data['guid'] = self.guid
        data['senderGUID'] = self.transport.guid
        data['uri'] = self.transport.uri
        data['pubkey'] = self.transport.pubkey
        data['senderNick'] = self.transport.nickname
        data['senderNamecoin'] = self.transport.namecoin_id
        data['v'] = constants.VERSION

        # Sign cleartext data
        sig_data = json.dumps(data).encode('hex')
        signature = self.sign(sig_data).encode('hex')

        self.log.datadump('Sending to peer: %s %s', self.address, pformat(data))

        try:
            # Encrypt signature and data
            data = self.encrypt(json.dumps({
                'sig': signature,
                'data': sig_data
            }))
        except Exception as exc:
            self.log.error('Encryption failed. %s', exc)
            return

        try:
            self.send_raw(data, callback)
        except Exception as exc:
            self.log.error("Was not able to send raw data: %s", exc)


class PeerListener(GUIDMixin):
    def __init__(self, ip, port, ctx, guid, data_cb):
        super(PeerListener, self).__init__(guid)

        self.ip = ip
        self.port = port
        self._data_cb = data_cb
        self.uri = network_util.get_peer_url(self.ip, self.port)
        self.is_listening = False
        self.ctx = ctx
        self.socket = None
        self.stream = None
        self._ok_msg = None

        self.log = logging.getLogger(self.__class__.__name__)

    def set_ip_address(self, new_ip):
        self.ip = new_ip
        self.uri = network_util.get_peer_url(self.ip, self.port)
        if not self.is_listening:
            return

        try:
            self.stream.close()
            self.listen()
        except Exception as e:
            self.log.error('[Requests] error: %s', e)

    def set_ok_msg(self, ok_msg):
        self._ok_msg = ok_msg

    def listen(self):
        self.log.info("Listening at: %s:%s", self.ip, self.port)
        self.socket = self.ctx.socket(zmq.REP)

        if network_util.is_loopback_addr(self.ip):
            try:
                # we are in local test mode so bind that socket on the
                # specified IP
                self.log.info("PeerListener.socket.bind('%s') LOOPBACK", self.uri)
                self.socket.bind(self.uri)
            except ZMQError as e:
                error_message = "".join([
                    "PeerListener.listen() error: ",
                    "Could not bind socket to %s. " % self.uri,
                    "Details:\n",
                    "(%s)" % e])

                if platform.system() == 'Darwin':
                    error_message.join([
                        "\n\nPerhaps you have not added a ",
                        "loopback alias yet.\n",
                        "Try this on your terminal and restart ",
                        "OpenBazaar in development mode again:\n",
                        "\n\t$ sudo ifconfig lo0 alias 127.0.0.2",
                        "\n\n"])
                raise Exception(error_message)
        elif '[' in self.ip:
            self.log.info("PeerListener.socket.bind('tcp://[*]:%s') IPV6", self.port)
            self.socket.ipv6 = True
            self.socket.bind('tcp://[*]:%s' % self.port)
        else:
            self.log.info("PeerListener.socket.bind('tcp://*:%s') IPV4", self.port)
            self.socket.bind('tcp://*:%s' % self.port)

        self.stream = zmqstream.ZMQStream(
            self.socket, io_loop=ioloop.IOLoop.current()
        )

        def handle_recv(messages):
            # FIXME: investigate if we really get more than one messages here
            for msg in messages:
                self._on_raw_message(msg)

            if self._ok_msg:
                self.stream.send(json.dumps(self._ok_msg))

        self.is_listening = True

        self.stream.on_recv(handle_recv)

    def _on_raw_message(self, serialized):
        self.log.info("connected %d", len(serialized))
        try:
            msg = json.loads(serialized[0])
        except ValueError:
            self.log.info("incorrect msg! %s", serialized)
            return

        self._data_cb(msg)


class CryptoPeerListener(PeerListener):

    def __init__(self, ip, port, pubkey, secret, ctx, guid, data_cb):

        super(CryptoPeerListener, self).__init__(ip, port, ctx, guid, data_cb)

        self.pubkey = pubkey
        self.secret = secret

        # FIXME: refactor this mess
        # this was copied as is from CryptoTransportLayer
        # soon all crypto code will be refactored and this will be removed
        self.cryptor = Cryptor(pubkey_hex=self.pubkey, privkey_hex=self.secret)

    @staticmethod
    def is_handshake(message):
        """
        Return whether message is a plaintext handshake

        :param message: serialized JSON
        :return: True if proper handshake message
        """
        try:
            message = json.loads(message)
        except (ValueError, TypeError):
            return False

        return 'type' in message

    def _on_raw_message(self, serialized):
        """
        Handles receipt of encrypted/plaintext message
        and passes to appropriate callback.

        :param serialized:
        :return:
        """
        if not self.is_handshake(serialized):

            try:
                message = self.cryptor.decrypt(serialized)
                message = json.loads(message)

                signature = message['sig'].decode('hex')
                signed_data = message['data']

                if CryptoPeerListener.validate_signature(signature, signed_data):
                    message = signed_data.decode('hex')
                    message = json.loads(message)

                    if message.get('guid') != self.guid:
                        return

                else:
                    return
            except RuntimeError as err:
                self.log.error('Could not decrypt message properly %s', err)
                return
            except Exception as exc:
                self.log.error('Cannot unpack data: %s', exc)
                return
        else:
            message = json.loads(serialized)

        self.log.debugv('Received message of type "%s"',
                        message.get('type', 'unknown'))
        self._data_cb(message)

    @staticmethod
    def validate_signature(signature, data):
        data_json = json.loads(data.decode('hex'))
        sig_cryptor = Cryptor(pubkey_hex=data_json['pubkey'])

        if sig_cryptor.verify(signature, data):
            return True
        else:
            return False
