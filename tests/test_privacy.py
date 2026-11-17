import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrivacyTests(unittest.TestCase):
    def test_gateway_disables_body_and_message_capture_without_silent_fallback(self):
        module = load_script("configure_apim")
        writes = []

        def az(*args):
            command = args[:3]
            if command == ("apim", "show", "-g"):
                return {"provisioningState": "Succeeded", "id": "/demo-apim",
                        "identity": {"principalId": "demo-principal"}, "gatewayUrl": "https://example.invalid"}
            if command == ("cognitiveservices", "account", "show"):
                return {"id": "/demo-ai"}
            if command == ("monitor", "log-analytics", "workspace"):
                return {"id": "/demo-logs"}
            if command == ("role", "assignment", "list"):
                return [{"id": "existing-role"}]
            if command == ("monitor", "diagnostic-settings", "categories"):
                return {"value": [{"name": "GatewayLogs"}, {"name": "GatewayLlmLogs"}]}
            raise AssertionError(f"Unexpected Azure CLI command: {command}")

        def arm(method, url, body=None):
            if method == "GET" and "/loggers?" in url:
                return {"value": [{"name": "azuremonitor"}]}
            self.assertEqual(method, "PUT")
            writes.append((url, json.loads(json.dumps(body))))
            return {}

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "azure.json"
            config.write_text(json.dumps({
                "resource_group": "demo", "apim": "demo", "ai_services": "demo",
                "log_analytics": "demo", "tenant_id": "demo", "gateway_app_id": "demo",
            }))
            with patch.object(module, "CONFIG_PATH", config), patch.object(module, "az", az), \
                    patch.object(module, "arm", arm), contextlib.redirect_stdout(io.StringIO()):
                module.main()
            diagnostics = [body["properties"] for url, body in writes if "/diagnostics/" in url]
            self.assertEqual(len(diagnostics), 2)
            for diagnostic in diagnostics:
                self.assertFalse(diagnostic["logClientIp"])
                for side in ("frontend", "backend"):
                    for direction in ("request", "response"):
                        self.assertEqual(diagnostic[side][direction]["body"]["bytes"], 0)
            llm = diagnostics[-1]["largeLanguageModel"]
            self.assertEqual(llm["logs"], "disabled")
            self.assertEqual(llm["requests"]["messages"], "none")
            self.assertEqual(llm["responses"]["messages"], "none")

            def failing_arm(method, url, body=None):
                if "2024-10-01-preview" in url:
                    raise RuntimeError("privacy settings rejected")
                return arm(method, url, body)

            with patch.object(module, "CONFIG_PATH", config), patch.object(module, "az", az), \
                    patch.object(module, "arm", failing_arm):
                with self.assertRaisesRegex(RuntimeError, "privacy settings rejected"):
                    module.main()

    def test_collector_does_not_request_settings_permission_or_unmask_names(self):
        module = load_script("m365_collector")
        calls = []

        def graph(method, path, body=None):
            calls.append((method, path, body))
            if path.startswith("/servicePrincipals(appId="):
                return {"id": "graph", "appRoles": [{"value": "Reports.Read.All", "id": "reports-read"}]}
            if path.startswith("/applications?"):
                return {"value": [{"id": "app", "appId": "client"}]}
            if path.startswith("/servicePrincipals?"):
                return {"value": [{"id": "sp"}]}
            if path.endswith("/appRoleAssignments"):
                return {"value": [{"appRoleId": "reports-read", "id": "assignment"}]}
            if path.endswith("?$select=passwordCredentials"):
                return {"passwordCredentials": []}
            if path.endswith("/addPassword"):
                return {"secretText": "test-only-placeholder"}
            raise AssertionError(f"Unexpected Graph request: {method} {path}")

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "azure.json"
            config.write_text("{}")
            with patch.object(module, "CONFIG_PATH", config), patch.object(module, "graph", graph), \
                    patch("sys.argv", ["m365_collector.py"]), contextlib.redirect_stderr(io.StringIO()):
                module.main()
        self.assertFalse(any("reportSettings" in path for _, path, _ in calls))
        self.assertFalse(any("ReportSettings.ReadWrite.All" in json.dumps(body) for _, _, body in calls))


if __name__ == "__main__":
    unittest.main()
