from __future__ import annotations

import asyncio
import os
import unittest
import uuid

# Force in-memory persistence before settings are cached.
os.environ["DEMO_MODE"] = "true"
os.environ.setdefault("BACKEND_INTERNAL_TOKEN", "test-token")

from app.conversation import (
    IDENTIFIER_HINT,
    _looks_abbreviated,
    _parse_identifiers,
    _product_confirmation,
    _selection_prompt,
    handle_photon_message,
)
from app.settings import get_settings
from app.storage import list_owner_products, reset_memory_store, save_confirmed_product, set_conversation


class ConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        get_settings.cache_clear()
        reset_memory_store()
        self.owner = f"tester-{uuid.uuid4()}"

    def _send(self, text: str) -> str:
        event = {
            "message_id": str(uuid.uuid4()),
            "sender_id": self.owner,
            "space_id": f"space-{self.owner}",
            "content_type": "text",
            "text": text,
            "image": None,
        }
        result = asyncio.run(handle_photon_message(event))
        return result["reply"]

    def test_parse_identifiers(self) -> None:
        parsed = _parse_identifiers("MODEL AO10FK-A DATE 1521")
        self.assertEqual(parsed["model_number"], "AO10FK-A")
        self.assertEqual(parsed["date_code"], "1521")

    def test_help_does_not_leak_affected_identifiers(self) -> None:
        help_reply = self._send("HELP")
        self.assertIn("Lodestone", help_reply)
        self.assertIn(IDENTIFIER_HINT, help_reply)
        self.assertNotIn("AO10FK-A", help_reply)
        self.assertNotIn("1521", help_reply)
        self.assertIn("no saved products", self._send("LIST").casefold())

    def test_abbreviated_confirmation_copy(self) -> None:
        item = {"brand": None, "product_name": "LD RED 100 CT EA", "uncertain_fields": ["product_name"]}
        self.assertTrue(_looks_abbreviated(item))
        reply = _product_confirmation("receipt", [item])
        self.assertIn("looks abbreviated", reply.casefold())
        self.assertNotIn("product_name", reply)

    def test_selection_prompt_lists_items(self) -> None:
        items = [
            {"brand": "Pepsi", "product_name": "Diet Pepsi"},
            {"brand": None, "product_name": "LD RED 100 CT EA"},
        ]
        prompt = _selection_prompt(items)
        self.assertIn("Reply 1–2", prompt)
        self.assertIn("1. Pepsi — Diet Pepsi", prompt)

    def test_abbreviated_skips_cpsc_name_search(self) -> None:
        product = save_confirmed_product(
            owner_handle=self.owner,
            brand=None,
            product_name="LD RED 100 CT EA",
            identifiers={},
            source_type="receipt",
        )
        set_conversation(
            owner_handle=self.owner,
            state="awaiting_label",
            payload={
                "product_id": product["id"],
                "brand": None,
                "product_name": "LD RED 100 CT EA",
                "identifiers": {},
            },
        )
        reply = self._send("CHECK")
        self.assertIn("abbreviated", reply.casefold())
        self.assertIn("package label", reply.casefold())
        self.assertNotIn("AO10FK", reply)

    def test_no_match_includes_checked_at(self) -> None:
        product = save_confirmed_product(
            owner_handle=self.owner,
            brand="Acme",
            product_name="Kitchen Blender Pro",
            identifiers={},
            source_type="receipt",
        )
        set_conversation(
            owner_handle=self.owner,
            state="idle",
            payload={
                "last_product_id": product["id"],
                "brand": "Acme",
                "product_name": "Kitchen Blender Pro",
                "identifiers": {},
            },
        )
        # Force a completed no-match path without network by stubbing search in this process.
        import app.conversation as conversation_module

        async def fake_search(_query: str):
            return [], "", False

        original = conversation_module.search_recalls
        conversation_module.search_recalls = fake_search  # type: ignore[assignment]
        try:
            reply = self._send("CHECK")
        finally:
            conversation_module.search_recalls = original  # type: ignore[assignment]
        self.assertIn("checked at", reply.casefold())
        self.assertIn("does not establish", reply.casefold())

    def test_identifier_verification_flow(self) -> None:
        product = save_confirmed_product(
            owner_handle=self.owner,
            brand="myCharge",
            product_name="POWER HUB",
            identifiers={},
            source_type="receipt",
        )
        set_conversation(
            owner_handle=self.owner,
            state="awaiting_label",
            payload={
                "product_id": product["id"],
                "brand": "myCharge",
                "product_name": "POWER HUB",
                "identifiers": {},
            },
        )
        reply = self._send("MODEL AO10FK-A DATE 1523")
        self.assertIn("historical", reply.casefold())
        self.assertIn("cpsc.gov/recalls/2024/mycharge", reply.casefold())
        self.assertIn("not a claim of a current", reply.casefold())
        done = self._send("DONE")
        self.assertIn("complete", done.casefold())
        products = list_owner_products(owner_handle=self.owner)
        self.assertEqual(len(products), 1)

    def test_wrong_lot_is_not_affected(self) -> None:
        product = save_confirmed_product(
            owner_handle=self.owner,
            brand="myCharge",
            product_name="POWER HUB",
            identifiers={},
            source_type="receipt",
        )
        set_conversation(
            owner_handle=self.owner,
            state="awaiting_label",
            payload={"product_id": product["id"], "brand": "myCharge", "product_name": "POWER HUB", "identifiers": {}},
        )
        reply = self._send("MODEL AO10FK-A DATE 1524")
        self.assertNotIn("⚠", reply)
        self.assertIn("outside the affected", reply.casefold())
        self.assertIn("historical", reply.casefold())


if __name__ == "__main__":
    unittest.main()
