import { serve } from "@hono/node-server";
import dotenv from "dotenv";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

dotenv.config({ path: fileURLToPath(new URL("../../.env", import.meta.url)) });

const required = [
  "SPECTRUM_PROJECT_ID",
  "SPECTRUM_PROJECT_SECRET",
  "BACKEND_INTERNAL_TOKEN",
] as const;

for (const key of required) {
  if (!process.env[key]) throw new Error(`${key} must be set before starting the Spectrum gateway.`);
}

const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
const port = Number(process.env.SPECTRUM_PORT ?? "8001");
const allowedSenderIds = new Set(
  (process.env.ALLOWED_SENDER_IDS ?? "")
    .split(",")
    .map((senderId) => senderId.trim())
    .filter(Boolean),
);

if (allowedSenderIds.size === 0) {
  throw new Error("ALLOWED_SENDER_IDS must contain at least one exact Spectrum sender ID.");
}

const spectrum = await Spectrum({
  projectId: process.env.SPECTRUM_PROJECT_ID!,
  projectSecret: process.env.SPECTRUM_PROJECT_SECRET!,
  providers: [imessage.config()],
});

const app = new Hono();
app.get("/health", (c) => c.json({ status: "ok" }));
app.post("/webhook", (c) =>
  spectrum.webhook(c.req.raw, async (space: any, message: any) => {
    if (message.direction === "outbound" || space.type === "group") return;
    const senderId = String(message.sender?.id ?? space.id);
    if (!allowedSenderIds.has(senderId)) return;

    const incoming: Record<string, unknown> = {
      message_id: message.id,
      sender_id: senderId,
      space_id: space.id,
      content_type: message.content.type,
    };

    if (message.content.type === "text") {
      incoming.text = message.content.text;
    } else if (message.content.type === "attachment") {
      const mimeType = message.content.mimeType;
      if (!["image/jpeg", "image/png", "image/webp"].includes(mimeType)) {
        await space.send("Please send a JPEG, PNG, or WebP photo of the receipt or product label.");
        return;
      }
      const bytes = await message.content.read();
      if (bytes.byteLength > 10 * 1024 * 1024) {
        await space.send("That image is too large. Please send an image under 10 MB.");
        return;
      }
      incoming.image = {
        base64: Buffer.from(bytes).toString("base64"),
        mime_type: mimeType,
        name: message.content.name,
      };
    } else {
      return;
    }

    const response = await fetch(`${backendUrl}/api/photon/message`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-recall-radar-internal": process.env.BACKEND_INTERNAL_TOKEN!,
      },
      body: JSON.stringify(incoming),
    });
    if (!response.ok) {
      console.error("FastAPI message handler failed", response.status, await response.text());
      await space.send("I couldn't finish that check just now. Please try again shortly.");
      return;
    }
    const result = (await response.json()) as { reply?: string };
    if (result.reply) await space.send(result.reply);
  }),
);

serve({ fetch: app.fetch, port }, (info) => {
  console.log(`RecallRadar Spectrum gateway listening on http://localhost:${info.port}`);
});
