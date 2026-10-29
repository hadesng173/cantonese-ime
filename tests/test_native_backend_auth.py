"""Mock the actual Win32 authentication calls; never open a pipe or token."""
from __future__ import annotations

import ctypes
import threading
import unittest
from unittest import mock

from cantonese_ime.native_protocol import LookupRequest, Status, encode_request, frame
from native.backend import server


class ClientAuthenticationTests(unittest.TestCase):
    expected = ("S-1-5-21-1-2-3-1001", 7)

    def api(self, *, user=None, session=7, fail=None):
        kernel, advapi = mock.Mock(), mock.Mock()
        with mock.patch.object(server.ctypes, "WinDLL", create=True,
                               side_effect=[kernel, advapi]), \
             mock.patch.object(server.os, "name", "nt"):
            api = server.Win32()
        state = {"impersonating": False, "order": [], "user": user or self.expected[0],
                 "session": session}
        kernel.GetCurrentThread.return_value = 42
        kernel.LocalFree.side_effect = lambda _sid: state["order"].append("free_sid") or None
        kernel.CloseHandle.side_effect = lambda _token: state["order"].append("close_token") or True

        def impersonate(_handle):
            state["order"].append("impersonate")
            if fail == "impersonate":
                return False
            state["impersonating"] = True
            return True

        def open_token(_thread, _access, _as_self, token):
            state["order"].append("open_token")
            if fail == "open_token":
                return False
            token._obj.value = 0 if fail == "empty_token" else 501
            return True

        def information(_token, kind, output, _capacity, length):
            if kind == server.TOKEN_USER:
                state["order"].append("user_size" if output is None else "user")
                length._obj.value = ctypes.sizeof(server.SidAndAttributes)
                if output is None:
                    if fail == "user_size":
                        length._obj.value = 0
                    return False
                if fail == "user":
                    return False
                ctypes.cast(output, ctypes.POINTER(server.SidAndAttributes)).contents.Sid = 123
            elif kind == server.TOKEN_SESSION_ID:
                state["order"].append("session")
                if fail == "session":
                    return False
                output._obj.value = state["session"]
                length._obj.value = (1 if fail == "session_size" else ctypes.sizeof(server.w.DWORD))
            else:
                raise AssertionError("Unexpected token information class")
            return True

        def convert(_sid, output):
            state["order"].append("convert_sid")
            if fail == "convert_sid":
                return False
            output._obj.value = state["user"]
            return True

        def revert():
            state["order"].append("revert")
            if fail == "revert":
                return False
            state["impersonating"] = False
            return True

        advapi.ImpersonateNamedPipeClient.side_effect = impersonate
        advapi.OpenThreadToken.side_effect = open_token
        advapi.GetTokenInformation.side_effect = information
        advapi.ConvertSidToStringSidW.side_effect = convert
        advapi.RevertToSelf.side_effect = revert
        return api, state

    def test_identification_token_same_user_and_session_is_allowed_then_reverted(self):
        api, state = self.api()
        with mock.patch.object(server, "_last_error", return_value=server.ERROR_INSUFFICIENT_BUFFER):
            api.authenticate_client(100, self.expected)
        self.assertFalse(state["impersonating"])
        self.assertEqual(state["order"], ["impersonate", "open_token", "user_size", "user",
                                          "convert_sid", "free_sid", "session", "revert", "close_token"])
        opened = api.advapi.OpenThreadToken.call_args.args
        self.assertEqual(opened[:3], (42, server.TOKEN_QUERY, True))
        self.assertEqual(api.advapi.OpenThreadToken.argtypes,
                         [server.w.HANDLE, server.w.DWORD, server.w.BOOL,
                          ctypes.POINTER(server.w.HANDLE)])
        self.assertEqual(api.advapi.ImpersonateNamedPipeClient.argtypes, [server.w.HANDLE])
        self.assertEqual(api.advapi.RevertToSelf.argtypes, [])

    def test_wrong_user_or_session_is_denied_after_reverting_and_closing_token(self):
        for kwargs in ({"user": "S-1-5-21-4-5-6-1002"}, {"session": 8}):
            with self.subTest(**kwargs):
                api, state = self.api(**kwargs)
                with mock.patch.object(server, "_last_error", return_value=122):
                    with self.assertRaises(PermissionError):
                        api.authenticate_client(100, self.expected)
                self.assertFalse(state["impersonating"])
                self.assertEqual(state["order"][-2:], ["revert", "close_token"])

    def test_api_and_invalid_token_information_fail_closed_and_revert(self):
        for failure in ("impersonate", "open_token", "empty_token", "user_size", "user", "convert_sid",
                        "session", "session_size"):
            with self.subTest(failure=failure):
                api, state = self.api(fail=failure)
                with mock.patch.object(server, "_last_error", return_value=122):
                    with self.assertRaises((OSError, ValueError)):
                        api.authenticate_client(100, self.expected)
                self.assertFalse(state["impersonating"])
                self.assertEqual(api.advapi.RevertToSelf.call_count, 0 if failure == "impersonate" else 1)
                self.assertEqual(api.kernel.CloseHandle.call_count,
                                 0 if failure in ("impersonate", "open_token", "empty_token") else 1)

    def test_size_query_api_error_is_preserved_and_reverted(self):
        api, state = self.api()
        with mock.patch.object(server, "_last_error", return_value=5):
            with self.assertRaises(OSError) as result:
                api.authenticate_client(100, self.expected)
        self.assertEqual(result.exception.winerror, 5)
        self.assertFalse(state["impersonating"])
        self.assertEqual(state["order"][-2:], ["revert", "close_token"])

    def test_cleanup_api_errors_do_not_authorize_dispatch(self):
        for api_name, result in (("LocalFree", 1), ("CloseHandle", False)):
            with self.subTest(api_name=api_name):
                api, state = self.api()
                getattr(api.kernel, api_name).side_effect = None
                getattr(api.kernel, api_name).return_value = result
                with mock.patch.object(server, "_last_error", return_value=122):
                    with self.assertRaises(OSError):
                        api.authenticate_client(100, self.expected)
                self.assertFalse(state["impersonating"])
                api.advapi.RevertToSelf.assert_called_once()

    def test_broker_identity_uses_its_process_token_user_and_session(self):
        api, state = self.api()
        api.kernel.GetCurrentProcess.return_value = 43

        def open_token(_process, _access, token):
            token._obj.value = 502
            return True

        api.advapi.OpenProcessToken.side_effect = open_token
        with mock.patch.object(server, "_last_error", return_value=122):
            self.assertEqual(api.identity(), self.expected)
        self.assertEqual(api.advapi.OpenProcessToken.call_args.args[:2], (43, server.TOKEN_QUERY))
        self.assertEqual(state["order"][-1], "close_token")
        api.advapi.ImpersonateNamedPipeClient.assert_not_called()
        api.kernel.ProcessIdToSessionId.assert_not_called()

    def test_failed_revert_exits_process_without_cleanup_or_returning(self):
        class ProcessTerminated(BaseException):
            pass

        for failure in ("revert",):
            api, state = self.api(fail=failure)
            with mock.patch.object(server, "_last_error", return_value=122), \
                 mock.patch.object(server.os, "_exit", side_effect=ProcessTerminated) as terminate:
                with self.assertRaises(ProcessTerminated):
                    api.authenticate_client(100, self.expected)
            terminate.assert_called_once_with(1)
            self.assertTrue(state["impersonating"])
            self.assertEqual(state["order"][-1], "revert")
            api.kernel.CloseHandle.assert_not_called()

    def run_worker(self, api, state, *, change_after_first=False):
        stop = threading.Event()
        payload = encode_request(LookupRequest(1, 0, 0, 9, "ngho"))
        incoming = iter([frame(payload)[:4], payload] * (2 if change_after_first else 1))
        connections = []

        def connect(_handle, _stop):
            connections.append(1)
            if len(connections) == 2:
                stop.set()

        def read(_handle, _size, _stop):
            self.assertFalse(state["impersonating"])
            state["order"].append("read")
            return next(incoming)

        def dispatch(*_args):
            self.assertFalse(state["impersonating"])
            state["order"].append("dispatch")
            return {"status": Status.OK}

        def write(*_args):
            self.assertFalse(state["impersonating"])
            if change_after_first:
                state["user"] = "S-1-5-21-other-user"
            else:
                stop.set()

        api.connect, api.read_exact, api.write_all = connect, read, write
        log = lambda *_a, **_k: self.assertFalse(state["impersonating"])
        with mock.patch.object(server, "_last_error", return_value=122), \
             mock.patch.object(server, "dispatch", side_effect=dispatch) as dispatch_mock, \
             mock.patch.object(server, "encode_response", return_value=b"mock response"):
            server._run_pool(api, [100], object(), stop, log, lambda: None,
                             expected_identity=self.expected)
        return dispatch_mock.call_count

    def test_worker_authenticates_only_after_full_read_and_before_dispatch(self):
        api, state = self.api()
        self.assertEqual(self.run_worker(api, state), 1)
        self.assertEqual(state["order"][:3], ["read", "read", "impersonate"])
        self.assertLess(state["order"].index("revert"), state["order"].index("dispatch"))

    def test_worker_never_dispatches_wrong_user_session_or_authentication_error(self):
        for kwargs in ({"user": "S-1-5-21-other-user"}, {"session": 8},
                       {"fail": "impersonate"}, {"fail": "open_token"}, {"fail": "session"}):
            with self.subTest(**kwargs):
                api, state = self.api(**kwargs)
                self.assertEqual(self.run_worker(api, state), 0)

    def test_worker_authenticates_every_message_not_only_connection(self):
        api, state = self.api()
        self.assertEqual(self.run_worker(api, state, change_after_first=True), 1)
        self.assertEqual(api.advapi.ImpersonateNamedPipeClient.call_count, 2)


if __name__ == "__main__":
    unittest.main()
