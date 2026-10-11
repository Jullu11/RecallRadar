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
const linkedSenderIds = new Set<string>();

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
  const { texts, attachments } = flattenContent(message.content as ContentPart);
  const caption = texts.map((part) => part.trim()).filter(Boolean).join("\n");
  if (!allowedSenderIds.has(senderId) && !linkedSenderIds.has(senderId) && !/^LINK\s+\d{10}$/i.test(caption)) {
    try {
      const status = await fetch(`${backendUrl}/api/senders/linked`, {
        method: "POST",
        signal: AbortSignal.timeout(120_000),
        headers: { "content-type": "application/json", "x-lodestone-internal": process.env.BACKEND_INTERNAL_TOKEN! },
        body: JSON.stringify({ sender_id: senderId }),
      });
      const linked = status.ok && (await status.json() as { linked?: boolean }).linked;
      if (!linked) {
        console.warn(`Blocked sender: ${senderId} (not linked)`);
        return;
      }
      linkedSenderIds.add(senderId);
    } catch (error) {
      console.error("Could not verify sender link", error);
      return;
    }
  }

  const incoming: Record<string, unknown> = {
    message_id: message.id,
    sender_id: senderId,
    space_id: space.id,
  };

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

  console.log("Inbound message received");

  const response = await fetch(`${backendUrl}/api/photon/message`, {
    method: "POST",
        signal: AbortSignal.timeout(120_000),
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
  if (response.ok && /^LINK\s+\d{10}$/i.test(caption) && result.reply?.startsWith("Your Lodestone account is linked")) {
    linkedSenderIds.add(senderId);
  }
  if (result.reply) await space.send(result.reply);
}

const spectrum = await Spectrum({
  projectId: process.env.SPECTRUM_PROJECT_ID!,
  projectSecret: process.env.SPECTRUM_PROJECT_SECRET!,
  providers: [imessage.config()],
});

let dispatchingNotifications = false;
async function dispatchPendingNotifications(): Promise<void> {
  if (dispatchingNotifications) return;
  dispatchingNotifications = true;
  try {
    const response = await fetch(`${backendUrl}/api/notifications/pending`, {
      method: "POST",
        signal: AbortSignal.timeout(120_000),
      headers: {
        "content-type": "application/json",
        "x-lodestone-internal": process.env.BACKEND_INTERNAL_TOKEN!,
      },
      body: "{}",
    });
    if (!response.ok) {
      console.error("Notification queue request failed", response.status, await response.text());
      return;
    }
    const payload = (await response.json()) as {
      notifications?: Array<{ id: string; recipient: string; claim_token: string; message: string }>;
    };
    for (const notification of payload.notifications ?? []) {
      try {
        const space = await imessage(spectrum).space(notification.recipient);
        await space.send(notification.message);
        const ack = await fetch(`${backendUrl}/api/notifications/${encodeURIComponent(notification.id)}/sent?claim_token=${encodeURIComponent(notification.claim_token)}`, {
          method: "POST",
        signal: AbortSignal.timeout(120_000),
          headers: { "x-lodestone-internal": process.env.BACKEND_INTERNAL_TOKEN! },
        });
        if (!ack.ok) console.error("Notification acknowledgement failed", notification.id, ack.status);
      } catch (error) {
        console.error("Notification delivery failed", notification.id, error);
      }
    }
  } catch (error) {
    console.error("Notification poll failed", error);
  } finally {
    dispatchingNotifications = false;
  }
}

void dispatchPendingNotifications();
setInterval(() => void dispatchPendingNotifications(), 60_000);

// Health endpoint only — inbound iMessages use the long-lived Spectrum stream.
const app = new Hono();
app.get("/health", (c) => c.json({ status: "ok", mode: "stream" }));

serve({ fetch: app.fetch, port }, (info) => {
  console.log(`Lodestone health server on http://localhost:${info.port}`);
});

console.log("Listening for iMessages on Spectrum app.messages stream…");
console.log(`Allowed senders: ${[...allowedSenderIds].join(", ")}`);

for await (const [space, message] of spectrum.messages) {
  try {
    await handleInbound(space, message);
  } catch (error) {
    console.error("Failed to handle inbound message", error);
  }
}
