import json
import queue
import threading
import unittest
from unittest.mock import patch

from orchd.app_transport import RPC, RPCError


class Wire:
    def __init__(self):self.received=queue.Queue();self.sent=[]
    def send(self,raw):
        msg=json.loads(raw);self.sent.append(msg)
        if 'id' in msg and 'method' in msg:
            self.received.put(json.dumps({'method':'turn/completed','params':{'turn':{'id':'old'}}}))
            self.received.put(json.dumps({'id':msg['id'],'result':{}}))
    def recv(self):return self.received.get()
    def close(self):self.received.put('')


def endpoint_socket():
    """Stub the Unix socket helper RPC connects through (POSIX connect, or Winsock on Windows builds without the
    AF_UNIX binding); the real socket path is covered by tests/test_worker_socket.py."""
    return patch('orchd.app_transport.unix_socket.create'), patch('orchd.app_transport.unix_socket.connect')


class TransportTest(unittest.TestCase):
    def test_unsolicited_events_do_not_consume_rpc_response(self):
        wire=Wire()
        create, connect = endpoint_socket()
        with create as sock, connect as connected, \
             patch('websocket.create_connection',return_value=wire):
            rpc=RPC('/private/app.sock')
            self.assertEqual(rpc.call('turn/interrupt',{'threadId':'thread','turnId':'turn'}),{})
            self.assertEqual(rpc.events.get()['method'],'turn/completed')
            connected.assert_called_once_with(sock.return_value, '/private/app.sock', timeout=10)
            self.assertEqual(wire.sent[1]['method'],'initialized')
            rpc.close()

    def test_disconnect_wakes_waiter_with_uncertainty(self):
        wire=Wire()
        create, connect = endpoint_socket()
        with create, connect, patch('websocket.create_connection',return_value=wire):
            rpc=RPC('/private/app.sock');wire.received.put('');rpc.reader.join(1)
            with self.assertRaises(ConnectionError):rpc.call('thread/read',{})
            rpc.close()

    def test_rpc_rejection_preserves_structured_provider_error(self):
        error = {'code': -32000, 'message': 'Rejected',
                 'data': {'codexErrorInfo': 'usageLimitExceeded'}}
        class RejectingWire(Wire):
            def send(self, raw):
                msg = json.loads(raw)
                if msg.get('method') == 'turn/start':
                    self.received.put(json.dumps({'id': msg['id'], 'error': error}))
                else:
                    super().send(raw)
        wire = RejectingWire()
        create, connect = endpoint_socket()
        with create, connect, patch('websocket.create_connection', return_value=wire):
            rpc = RPC('/private/app.sock')
            try:
                with self.assertRaises(RPCError) as raised:
                    rpc.call('turn/start', {})
                self.assertEqual(raised.exception.error, error)
            finally:
                rpc.close()
