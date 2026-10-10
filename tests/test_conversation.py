from __future__ import annotations

import asyncio
import os
import unittest
import uuid

# Force in-memory persistence before settings are cached.
os.environ["DEMO_MODE"] = "true"
os.environ.setdefault("BACKEND_INTERNAL_TOKEN", "test-token")

from app.conversation import _parse_identifiers, handle_photon_message
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

    def test_help_and_list(self) -> None:
        help_reply = self._send("HELP")
        self.assertIn("RecallRadar", help_reply)
        self.assertIn("LIST", help_reply)
        self.assertIn("no saved products", self._send("LIST").casefold())

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
        self.assertIn("affected product", reply.casefold())
        self.assertIn("cpsc.gov/recalls/2024/mycharge", reply.casefold())
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
        self.assertNotIn("affected product", reply.casefold())
        self.assertIn("outside the affected", reply.casefold())


if __name__ == "__main__":
    unittest.main()
