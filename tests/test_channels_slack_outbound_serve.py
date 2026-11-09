import unittest
from unittest.mock import patch
from orchd.channels.serve import slack_outbound


class SlackOutboundServeTest(unittest.TestCase):
    def test_explicit_source_opt_in(self):
        source = {'id': 'slack-main'}
        self.assertIsNone(slack_outbound({}, source, None, None, None))
        self.assertIsNone(slack_outbound({'slack': {'other': {'outbound_enabled': True}}}, source, None, None, None))
        with patch('orchd.channels.serve.OutboundAdapter') as adapter:
            result = slack_outbound({'slack': {'slack-main': {'outbound_enabled': True}}}, source, 'session', 'caller', 'secrets')
            self.assertIs(result, adapter.return_value)
            self.assertEqual(adapter.call_args.args[:4], ('session', 'slack-main', 'caller', 'secrets'))
        with self.assertRaises(ValueError):
            slack_outbound({'slack': {'slack-main': {'outbound_enabled': 'true'}}}, source, None, None, None)
