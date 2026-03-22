import json
import sys
import types
from unittest import TestCase


def _install_lark_stub():
    lark_module = types.ModuleType("lark_oapi")
    api_module = types.ModuleType("lark_oapi.api")
    im_module = types.ModuleType("lark_oapi.api.im")
    v1_module = types.ModuleType("lark_oapi.api.im.v1")
    model_module = types.ModuleType("lark_oapi.api.im.v1.model")

    class _Builder:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

        def build(self):
            return object()

    class _Request:
        @staticmethod
        def builder():
            return _Builder()

    model_module.CreateMessageRequest = _Request
    model_module.CreateMessageRequestBody = _Request
    model_module.PatchMessageRequest = _Request
    model_module.PatchMessageRequestBody = _Request
    model_module.ReplyMessageRequest = _Request
    model_module.ReplyMessageRequestBody = _Request

    class _Client:
        pass

    lark_module.Client = _Client

    sys.modules.setdefault("lark_oapi", lark_module)
    sys.modules.setdefault("lark_oapi.api", api_module)
    sys.modules.setdefault("lark_oapi.api.im", im_module)
    sys.modules.setdefault("lark_oapi.api.im.v1", v1_module)
    sys.modules.setdefault("lark_oapi.api.im.v1.model", model_module)


_install_lark_stub()

from feishu_client import _card_json


class CardJsonTests(TestCase):
    def test_card_json_downgrades_markdown_tables_to_plain_text(self):
        content = (
            "# Result\n\n"
            "| Name | Value |\n"
            "| --- | --- |\n"
            "| foo | bar |\n"
            "| baz | qux |\n"
        )

        payload = json.loads(_card_json(content))
        rendered = payload["body"]["elements"][0]["content"]

        self.assertIn("Name / Value", rendered)
        self.assertIn("foo / bar", rendered)
        self.assertIn("baz / qux", rendered)
        self.assertNotIn("| Name | Value |", rendered)
        self.assertNotIn("| foo | bar |", rendered)
