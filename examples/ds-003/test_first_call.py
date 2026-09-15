import contextlib
import io
import unittest
from unittest.mock import patch
import first_call

class FirstCallTest(unittest.TestCase):
    def test_default_never_connects_or_reads_key(self):
        with patch.object(first_call.urllib.request, 'urlopen') as send, patch.object(first_call.getpass, 'getpass') as key:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(first_call.main([]), 0)
            send.assert_not_called()
            key.assert_not_called()
            self.assertIn('通信しません', output.getvalue())
    def test_no_key_never_sends(self):
        with patch.dict(first_call.os.environ, {}, clear=True), patch.object(first_call.getpass, 'getpass', return_value=''), patch.object(first_call.urllib.request, 'urlopen') as send:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(first_call.main(['--send']), 1)
            send.assert_not_called()
    def test_output_limit(self):
        self.assertEqual(first_call.payload()['max_tokens'], 256)
        self.assertEqual(first_call.payload()['thinking']['type'], 'disabled')
if __name__ == '__main__':
    unittest.main()
