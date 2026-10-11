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
// ALLOWED_SENDER_IDS=* opens the gateway to every Spectrum sender (hackathon / public demo).
const allowAllSenders = allowedSenderIds.has("*");

if (!allowAllSenders && allowedSenderIds.size === 0) {
  throw new Error(
    "ALLOWED_SENDER_IDS must contain at least one exact Spectrum sender ID, or * to allow anyone.",
  );
}

type ContentPart = {
  type?: string;
  text?: string;
  mimeType?: string;
  name?: string | null;
  read?: () => Promise<Uint8Array>;
  items?: ContentPart[];
};

function flattenContent(content: ContentPart): { texts: string[]; attachments: ContentPart[] } {
  const texts: string[] = [];
  const attachments: ContentPart[] = [];

  const visit = (node: ContentPart | undefined) => {
    if (!node) return;
    if (node.type === "text" && typeof node.text === "string") {
      texts.push(node.text);
      return;
    }
    if (node.type === "attachment") {
      attachments.push(node);
      return;
    }
    if (Array.isArray(node.items)) {
      for (const child of node.items) visit(child);
    }
  };

  visit(content);
  return { texts, attachments };
}

async function handleInbound(space: any, message: any): Promise<void> {
  if (message.direction === "outbound") return;
  if (space.type === "group") return;

  const senderId = String(message.sender?.id ?? space.id);
  if (!allowAllSenders && !allowedSenderIds.has(senderId)) {
    console.warn(`Blocked sender: ${senderId} (not in ALLOWED_SENDER_IDS)`);
    return;
  }

  const incoming: Record<string, unknown> = {
    message_id: message.id,
    sender_id: senderId,
    space_id: space.id,
  };

  const { texts, attachments } = flattenContent(message.content as ContentPart);
  const caption = texts.map((part) => part.trim()).filter(Boolean).join("\n");

  if (attachments.length > 0) {
    const attachment = attachments[0];
    const mimeType = attachment.mimeType;
    if (!mimeType || !["image/jpeg", "image/png", "image/webp"].includes(mimeType)) {
      await space.send("Please send a JPEG, PNG, or WebP photo of the receipt or product label.");
      return;
    }
    if (!attachment.read) {
      await space.send("I couldn't read that attachment. Please resend the photo.");
      return;
    }
    const bytes = await attachment.read();
    if (bytes.byteLength > 10 * 1024 * 1024) {
      await space.send("That image is too large. Please send an image under 10 MB.");
      return;
    }
    incoming.content_type = "attachment";
    if (caption) incoming.text = caption;
    incoming.image = {
      base64: Buffer.from(bytes).toString("base64"),
      mime_type: mimeType,
      name: attachment.name ?? null,
    };
  } else if (message.content.type === "text") {
    incoming.content_type = "text";
    incoming.text = message.content.text;
  } else if (caption) {
    incoming.content_type = "text";
    incoming.text = caption;
  } else {
    return;
  }

  console.log(`Inbound from ${senderId}: ${String(incoming.text ?? "[attachment]").slice(0, 80)}`);

  const response = await fetch(`${backendUrl}/api/photon/message`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-lodestone-internal": process.env.BACKEND_INTERNAL_TOKEN!,
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
}

const spectrum = await Spectrum({
  projectId: process.env.SPECTRUM_PROJECT_ID!,
  projectSecret: process.env.SPECTRUM_PROJECT_SECRET!,
  providers: [imessage.config()],
});

// Health endpoint only — inbound iMessages use the long-lived Spectrum stream.
const app = new Hono();
app.get("/health", (c) => c.json({ status: "ok", mode: "stream" }));

serve({ fetch: app.fetch, port }, (info) => {
  console.log(`Lodestone health server on http://localhost:${info.port}`);
});

console.log("Listening for iMessages on Spectrum app.messages stream…");
console.log(
  allowAllSenders
    ? "Allowed senders: * (anyone)"
    : `Allowed senders: ${[...allowedSenderIds].join(", ")}`,
);

for await (const [space, message] of spectrum.messages) {
  try {
    await handleInbound(space, message);
  } catch (error) {
    console.error("Failed to handle inbound message", error);
  }
}
