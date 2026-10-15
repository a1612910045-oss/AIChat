"""Authentication boundaries and the GPT stream/result contract; no live requests."""
import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from core import chatgpt_auth as ca, engine


def profile(expired=False):
    return {"client_id": "oaiapp_test", "subject": "test-user", "email": "test@example.invalid",
            "access_token": "fake-access-secret", "refresh_token": "fake-refresh-secret",
            "expires_at": time.time() + (-100 if expired else 3600), "scopes": [ca.PLAN_SCOPE]}


def result_json(**changes):
    data = {"true_intent": "casual_chat", "best_action": "make_plan", "she_needs": "action",
            "danger_level": 1, "literal_question": True, "should_reply_now": True,
            "tension_resolved": True, "candidates": ["好，明天见", "行啊，几点", "可以，明天聊"],
            "best_index": 1}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def completed(content=""):
    return NS(type="response.completed", response=NS(output_text=content, usage=None))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ca.AuthManager(Path(self.temp.name))

    def seed(self, record=None):
        record = record or profile()
        self.manager._save({"accounts": [record], "active": record["client_id"]})

    def test_dpapi_storage_and_stable_host(self):
        self.seed()
        encrypted = self.manager.credentials.read_bytes()
        self.assertNotIn(b"fake-access-secret", encrypted)
        self.assertNotIn(b"fake-refresh-secret", encrypted)
        self.assertEqual(self.manager._load()["accounts"][0]["access_token"], "fake-access-secret")
        self.assertEqual(self.manager.host_id(), self.manager.host_id())

    def test_pkce_and_dynamic_registration(self):
        flow = self.manager.new_login()
        params = parse_qs(urlparse(flow.authorization_url("http://127.0.0.1:1455/auth/callback")).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [expected])
        self.assertEqual(params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(params["agent_name_hint"], [ca.APP_NAME])
        self.assertIn(ca.PLAN_SCOPE, params["scope"][0])
        self.assertEqual(params["resource"], [ca.RESOURCE])

    def test_callback_rejects_state_duplicates_and_invalid_client(self):
        flow = self.manager.new_login()
        self.assertFalse(flow.accept_callback("state=wrong&code=x&client_id=oaiapp_a"))
        self.assertFalse(flow.accept_callback("state=%E4%B8%AD%E6%96%87&code=x"))
        self.assertFalse(flow.accept_callback(urlencode({"state": flow.state}) + "&code=a&code=b"))
        self.assertIsNone(flow.result)
        self.assertTrue(flow.accept_callback(urlencode({"state": flow.state, "code": "x"})))
        self.assertIn("error", flow.result)
        self.seed()
        returning = self.manager.new_login("oaiapp_test")
        returning.accept_callback(urlencode({"state": returning.state, "code": "x", "client_id": "oaiapp_other"}))
        self.assertIn("error", returning.result)

    def test_declined_authorization_is_not_exchanged(self):
        flow = self.manager.new_login()

        def browser(url):
            flow.accept_callback(urlencode({"state": flow.state, "error": "access_denied"}))
            return True

        with patch.object(ca.webbrowser, "open", side_effect=browser), patch.object(self.manager, "_post_token") as exchange:
            with self.assertRaises(ca.AuthError):
                flow.run()
        exchange.assert_not_called()
        self.assertFalse(self.manager.status()["enabled"])

    def test_new_client_exchange_and_permission_gate(self):
        flow = self.manager.new_login()

        def browser(url):
            flow.accept_callback(urlencode({"state": flow.state, "code": "test-code", "client_id": "oaiapp_issued"}))
            return True

        tokens = {"access_token": "new-token", "refresh_token": "new-refresh", "id_token": "signed-id",
                  "expires_in": 3600, "scope": "openid email offline_access"}
        with patch.object(ca.webbrowser, "open", side_effect=browser), \
             patch.object(self.manager, "_post_token", return_value=tokens) as exchange, \
             patch.object(self.manager, "validate_identity", return_value={"sub": "verified", "email": "verified@example.invalid"}) as validate:
            status = flow.run()
        form = exchange.call_args.args[0]
        self.assertEqual(form["client_id"], "oaiapp_issued")
        self.assertEqual(form["code_verifier"], flow.verifier)
        self.assertTrue(form["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertTrue(form["redirect_uri"].endswith("/auth/callback"))
        validate.assert_called_once_with("signed-id", "oaiapp_issued", flow.nonce)
        self.assertTrue(status["connected"])
        self.assertFalse(status["enabled"])
        with self.assertRaises(ca.AuthError):
            self.manager.access_token()

    def test_returning_client_and_expired_exchange_retry(self):
        retry = self.manager.new_login(pending_client_id="oaiapp_issued")
        params = parse_qs(urlparse(retry.authorization_url("http://127.0.0.1:1234/auth/callback")).query)
        self.assertEqual(params["client_id"], ["oaiapp_issued"])
        self.assertNotIn("agent_name_hint", params)
        retry.accept_callback(urlencode({"state": retry.state, "code": "new-code"}))
        self.assertEqual(retry.result["client_id"], "oaiapp_issued")

    def test_refresh_rotates_tokens_and_persists_together(self):
        self.seed(profile(expired=True))
        tokens = {"access_token": "rotated-access", "refresh_token": "rotated-refresh",
                  "expires_in": 3600, "scope": ca.PLAN_SCOPE}
        with patch.object(self.manager, "_post_token", return_value=tokens) as exchange:
            self.assertEqual(self.manager.access_token(), "rotated-access")
        stored = self.manager._load()["accounts"][0]
        self.assertEqual(stored["refresh_token"], "rotated-refresh")
        self.assertEqual(exchange.call_args.args[0]["client_id"], "oaiapp_test")
        self.assertNotIn("scope", exchange.call_args.args[0])

    def test_temporary_refresh_preserves_credentials_terminal_error_clears(self):
        self.seed(profile(expired=True))
        with patch.object(self.manager, "_post_token", side_effect=ca.AuthError("temporary")):
            with self.assertRaises(ca.AuthError):
                self.manager.access_token()
        self.assertEqual(self.manager._load()["accounts"][0]["refresh_token"], "fake-refresh-secret")
        error = ca.AuthError("expired")
        error.terminal = True
        with patch.object(self.manager, "_post_token", side_effect=error):
            with self.assertRaises(ca.AuthError):
                self.manager.access_token()
        stored = self.manager._load()["accounts"][0]
        self.assertNotIn("refresh_token", stored)
        self.assertEqual(stored["client_id"], "oaiapp_test")

    def test_model_catalog_uses_subscriber_slugs_and_order(self):
        self.seed()
        response = Mock(status_code=200)
        response.json.return_value = {"models": [
            {"slug": "gpt-second", "display_name": "Second", "visibility": "list"},
            {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
            {"slug": "gpt-first", "display_name": "First", "visibility": "list"},
        ]}
        with patch.object(ca.httpx, "get", return_value=response) as request:
            models = self.manager.list_models()
        self.assertEqual([m["slug"] for m in models], ["gpt-second", "gpt-first"])
        self.assertEqual(request.call_args.args[0], ca.RESOURCE + "/models")

    def test_logout_revoke_preserves_registration(self):
        self.seed()
        metadata = {"revocation_endpoint": ca.ISSUER + "/api/accounts/oauth/revoke"}
        with patch.object(self.manager, "discovery", return_value=metadata), \
             patch.object(ca.httpx, "post", return_value=Mock(status_code=200)) as revoke:
            self.manager.sign_out()
        self.assertEqual(revoke.call_args.kwargs["data"]["token_type_hint"], "refresh_token")
        self.assertFalse(self.manager.status()["connected"])
        self.assertEqual(self.manager.accounts()[0]["client_id"], "oaiapp_test")

    def test_signed_identity_checks_nonce_audience_and_expiration(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        public["kid"] = "test-key"
        response = Mock()
        response.json.return_value = {"keys": [public]}
        claims = {"sub": "test-user", "aud": "oaiapp_test", "iss": ca.ISSUER,
                  "iat": int(time.time()), "exp": int(time.time()) + 600, "nonce": "expected"}

        def token(**changes):
            return jwt.encode({**claims, **changes}, key, algorithm="RS256", headers={"kid": "test-key"})

        with patch.object(ca.AuthManager, "discovery", return_value={"jwks_uri": ca.ISSUER + "/.well-known/jwks.json"}), \
             patch.object(ca.httpx, "get", return_value=response):
            self.assertEqual(self.manager.validate_identity(token(), "oaiapp_test", "expected")["sub"], "test-user")
            for changes in ({"nonce": "wrong"}, {"aud": "other"}, {"exp": int(time.time()) - 100}, {"iss": "https://evil.invalid"}):
                with self.subTest(changes=changes), self.assertRaises(ca.AuthError):
                    self.manager.validate_identity(token(**changes), "oaiapp_test", "expected")


class EngineTests(unittest.TestCase):
    def test_combined_judgment_and_recommendation(self):
        result = engine.parse_result(result_json(), [("her", "明天聊吗")], None, 10)
        self.assertEqual(result["best_reply"], "行啊，几点")
        self.assertEqual(result["answers"]["danger_level"]["score"], 1)
        self.assertEqual(result["scores"], [])  # No invented Jev-style probabilities.

    def test_malformed_judgments_do_not_produce_recommendations(self):
        for changes in ({"true_intent": "invented"}, {"danger_level": True}, {"danger_level": 10},
                        {"candidates": ["one"]}, {"best_index": True}, {"should_reply_now": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ca.AuthError):
                engine.parse_result(result_json(**changes), [("her", "你好")], None, 10)

    def test_sanitization_preserves_recommended_original_index(self):
        result = engine.parse_result(result_json(candidates=["行啊", "行啊", "可以，几点"], best_index=2),
                                     [("her", "明天聊吗")], None, 10)
        self.assertEqual(result["candidates"], ["行啊", "可以，几点"])
        self.assertEqual(result["best_index"], 1)

    def test_partial_failed_and_incomplete_streams_are_not_success(self):
        delta = NS(type="response.output_text.delta", delta=result_json())
        for terminal in ([], [NS(type="response.incomplete")],
                         [NS(type="response.failed", response=NS(error=NS(code="subscription_sharing_usage_limit_exceeded")))]):
            with self.subTest(terminal=terminal), self.assertRaises(ca.AuthError):
                engine.consume_stream([delta, *terminal])
        text, _ = engine.consume_stream([delta, completed()])
        self.assertEqual(text, result_json())

    def test_quota_failure_pauses_capture(self):
        error = ca.api_error(None, "subscription_sharing_usage_limit_exceeded")
        self.assertTrue(error.pause_capture)
        self.assertFalse(ca.api_error(503).pause_capture)

    def test_oauth_responses_request_uses_supported_route_without_api_billing_fallback(self):
        import openai
        stream = Mock()
        stream.__enter__ = Mock(return_value=iter([completed(result_json())]))
        stream.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.responses.create.return_value = stream
        with patch.object(engine.auth, "status", return_value={"client_id": "oaiapp_test"}), \
             patch.object(engine.auth, "access_token", return_value="fake-oauth-access"), \
             patch.object(openai, "OpenAI", return_value=client) as create:
            result = engine.analyze([("her", "明天聊吗")], "friends", model="chosen-gpt")
        self.assertEqual(create.call_args.kwargs["api_key"], "fake-oauth-access")
        self.assertEqual(create.call_args.kwargs["base_url"], "https://api.openai.com/v1")
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "chosen-gpt")
        self.assertTrue(request["stream"])
        self.assertFalse(request["store"])
        self.assertNotIn("temperature", request)
        self.assertNotIn("max_output_tokens", request)
        self.assertEqual(result["best_index"], 1)


if __name__ == "__main__":
    unittest.main()
