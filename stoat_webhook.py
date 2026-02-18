import json
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import aiohttp
from loguru import logger


@dataclass
class StoatEmbed:
    """Representation of a sendable embed for Stoat/Revolt webhooks.

    This matches the `SendableEmbed` schema from the official Stoat API reference:
    https://developers.stoat.chat/api-reference/#model/sendableembed

    Only the documented fields are included. Unsupported or undocumented fields
    (like image/thumbnail objects, author/footer sub-objects) are omitted to prevent
    silent failures or ignored data.

    When `media` is set to a valid Autumn file ID, the embed becomes an "Image" type
    and displays the attached image as main content. External URLs are **not** supported
    in `media` — only internal file IDs.
    """

    title: Optional[str] = None
    """Embed title.

    Constraints:
        - min length: 1
        - max length: 100
    Example: "Event Announcement"
    """

    description: Optional[str] = None
    """Main embed body text (supports basic markdown).

    Constraints:
        - min length: 1
        - max length: 2000
    Example: "Picora’s Photo Decor Celebration Event!\\nFollow @trickcal_en to join."
    """

    url: Optional[str] = None
    """URL that the embed title and sometimes image link to.

    Constraints:
        - min length: 1
        - max length: 256
    Example: "https://trickcal.biligames.com/en/"
    """

    colour: Optional[str] = None
    """Primary colour of the embed (left border and accents).

    Also accepted as `color` (alias).

    Constraints:
        - min length: 1
        - max length: 128
        - Pattern: (?i)^(?:[a-z ]+|var\\(--[a-z\\d-]+\\)|rgba?\\([\\d, ]+\\)|#[a-f0-9]+|(repeating-)?(linear|conic|radial)-gradient\\(([a-z ]+|var\\(--[a-z\\d-]+\\)|rgba?\\([\\d, ]+\\)|#[a-f0-9]+|\\d+deg)([ ]+(\\d{1,3}%|0))?(,[ ]*([a-z ]+|var\\(--[a-z\\d-]+\\)|rgba?\\([\\d, ]+\\)|#[a-f0-9]+)([ ]+(\\d{1,3}%|0))?)+\\))$

    Valid examples:
        - "#FF5733" (hex)
        - "#f00" (short hex)
        - "hotpink" (named colour)
        - "rgba(255, 105, 180, 0.7)" (rgba)
        - "linear-gradient(to right, #667eea, #764ba2)" (gradient)
        - "var(--primary)" (CSS variable)
    """

    icon_url: Optional[str] = None
    """URL of a small icon displayed next to the title (limited support).

    Constraints:
        - min length: 1
        - max length: 256
    Note: This is **not** the same as author.icon_url — it's a legacy field that
    may only appear in certain embed renderings.
    Example: "https://example.com/small-icon.png"
    """

    media: Optional[str] = None
    """Autumn file ID of the main image/media.

    Must be a valid attachment ID obtained from uploading to Autumn.
    External URLs are **not** supported here.

    When set, changes embed type to "Image" and displays the file as main content.
    Example: "01JABCDEFGHJKLMNPQRSTUV"
    """


@dataclass
class StoatMasquerade:
    """Webhook sender override (masquerade) settings.

    Allows changing the displayed name, avatar and name colour of the webhook message.
    """

    name: Optional[str] = None
    """Custom username to display instead of the webhook's default name."""

    avatar: Optional[str] = None
    """URL of the avatar to display.

    External https URLs are supported here (unlike in embeds).
    Example: "https://pbs.twimg.com/profile_images/1925849773870854144/mwiH5RlP_400x400.jpg"
    """

    colour: Optional[str] = None
    """Custom colour for the displayed name (same format as embed.colour)."""


@dataclass
class StoatWebhookPayload:
    """Root payload for POST /webhooks/{id}/{token}

    Matches the documented fields accepted by the Stoat webhook endpoint.
    https://developers.stoat.chat/api-reference/#tag/webhooks/POST/webhooks/{webhook_id}/{token}
    """

    content: Optional[str] = None
    """Plain text content of the message.

    If this contains only a URL (e.g. tweet/X link), Stoat usually generates
    an automatic embed preview including image(s).
    """

    embeds: List[StoatEmbed] = field(default_factory=list)
    """List of rich embeds to include in the message.

    Maximum practical limit is usually 1–4 (rendering degrades after ~5).
    """

    masquerade: Optional[StoatMasquerade] = None
    """Override the webhook's displayed name, avatar and colour."""

    attachments: List[str] = field(default_factory=list)
    """List of Autumn file IDs to attach to the message (shown below text/embeds).

    Example: ["01JABC...", "01JDEF..."]
    """


class StoatWebhook:
    """Async webhook client with discord-webhook-like interface"""

    def __init__(
            self,
            webhook_url: str,
            *,
            rate_limit_retry: bool = True,
            session: Optional[aiohttp.ClientSession] = None
    ):
        self.webhook_url = webhook_url.rstrip("/")
        match = re.match(r".*/webhooks/([^/]+)/(.+?)(?:/|$)", self.webhook_url)
        if not match:
            raise ValueError("Invalid Stoat webhook URL format")
        self.id = match.group(1)  # webhook id
        self.token = match.group(2)  # webhook token

        self._session = session or aiohttp.ClientSession()
        self.rate_limit_retry = rate_limit_retry

        # Mutable state (like discord-webhook)
        self.embeds: List[StoatEmbed] = []
        self.content: Optional[str] = None
        self.masquerade: Optional[StoatMasquerade] = None
        self.attachments: List[str] = []  # file IDs
        self.last_response: Optional[aiohttp.ClientResponse] = None
        self.last_message_id: Optional[str] = None

        self.max_length = 2000

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def add_embed(self, embed: StoatEmbed) -> None:
        self.embeds.append(embed)

    def set_content(self, content: str) -> None:
        self.content = content

    def set_masquerade(self, masquerade: StoatMasquerade) -> None:
        self.masquerade = masquerade

    def add_attachment_id(self, file_id: str) -> None:
        self.attachments.append(file_id)

    def remove_embeds(self) -> None:
        self.embeds.clear()

    def remove_attachments(self) -> None:
        self.attachments.clear()

    def add_masquerade(self, masquerade: StoatMasquerade) -> None:
        self.masquerade = masquerade

    def byte_len(self, s: str) -> int:
        return len(s.encode("utf-8"))

    def truncate_to_bytes(self, s: str, max_bytes: int, suffix: str = "...") -> str:
        suffix_bytes = len(suffix.encode("utf-8"))
        limit = max_bytes - suffix_bytes
        encoded = s.encode("utf-8")
        if len(encoded) <= max_bytes:
            return s
        # Truncate to limit bytes, then decode safely ignoring partial chars
        return encoded[:limit].decode("utf-8", errors="ignore") + suffix

    def _build_payload(self) -> Dict:

        payload: Dict[str, Any] = {}
        available_max_length = self.max_length

        if self.content is not None:
            content = self.content
            self.content = self.truncate_to_bytes(content, available_max_length)
            payload["content"] = self.content
            available_max_length -= self.byte_len(content)

        if self.masquerade:
            masquerade_dict = {}

            # name: max 32
            if self.masquerade.name and len(self.masquerade.name) > 32:
                self.masquerade.name = self.masquerade.name[:29] + "..."
            if self.masquerade.name:
                masquerade_dict["name"] = self.masquerade.name

            # avatar: max 256
            if self.masquerade.avatar and len(self.masquerade.avatar) > 256:
                self.masquerade.avatar = self.masquerade.avatar[:253] + "..."
            if self.masquerade.avatar:
                masquerade_dict["avatar"] = self.masquerade.avatar

            # colour: max 128 (hex almost never exceeds)
            if self.masquerade.colour and len(self.masquerade.colour) > 128:
                self.masquerade.colour = self.masquerade.colour[:128]
            if self.masquerade.colour:
                masquerade_dict["colour"] = self.masquerade.colour

            if masquerade_dict:
                payload["masquerade"] = masquerade_dict

        if self.embeds:
            payload_embeds = []

            for embed in self.embeds:
                embed_dict = {}

                # title: max 100
                if embed.title and len(embed.title) > 100:
                    embed.title = embed.title[:97] + "..."
                if embed.title:
                    embed_dict["title"] = embed.title

                # description: max 2000 ? doubt
                if embed.description:
                    embed.description = self.truncate_to_bytes(embed.description, available_max_length)
                    available_max_length -= self.byte_len(embed.description)
                    embed_dict["description"] = embed.description

                # url: max 256
                if embed.url and len(embed.url) > 256:
                    embed.url = embed.url[:253] + "..."
                if embed.url:
                    embed_dict["url"] = embed.url

                # colour / color: max 128
                colour_value = embed.colour
                if colour_value and len(colour_value) > 128:
                    colour_value = colour_value[:128]
                if colour_value:
                    embed_dict["colour"] = colour_value  # normalize key

                # icon_url: max 256
                if embed.icon_url and len(embed.icon_url) > 256:
                    embed.icon_url = embed.icon_url[:253] + "..."
                if embed.icon_url:
                    embed_dict["icon_url"] = embed.icon_url

                # media: file ID – no length limit documented
                if embed.media:
                    embed_dict["media"] = embed.media

                if embed_dict:
                    payload_embeds.append(embed_dict)

            if payload_embeds:
                payload["embeds"] = payload_embeds

        if self.attachments:
            if len(self.attachments) > 20:  # practical limit
                self.attachments = self.attachments[:20]
            payload["attachments"] = self.attachments

        return payload

    async def execute(
            self,
            remove_embeds: bool = False,
            remove_attachments: bool = False,
            clear_state: bool = False
    ) -> aiohttp.ClientResponse:
        """
        Execute the webhook with the current state (content, embeds, masquerade, attachments).

        Mimics discord-webhook.Webhook.execute behavior:
        - Returns the aiohttp.ClientResponse
        - Handles 200/204 as success
        - Logs errors
        - Optional: clears embeds/attachments after send
        - Parses response JSON if available and updates internal state

        :param remove_embeds: Clear stored embeds after execution
        :param remove_attachments: Clear stored attachment IDs after execution
        :param clear_state: Clear all mutable state (content, embeds, masquerade)
        :return: aiohttp.ClientResponse object
        """
        payload = self._build_payload()

        async with self._session.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                raise_for_status=False
        ) as response:
            self.last_response = response

            text = await response.text()

            if response.status in (200, 204):
                logger.debug("Stoat webhook executed successfully")
            elif response.status == 429 and self.rate_limit_retry:
                # Very basic retry – in production you should parse Retry-After header
                logger.warning("Rate limited (429). Consider waiting and retrying.")
                # For real retry: await asyncio.sleep(retry_after); then re-call
            else:
                logger.error(
                    f"Stoat webhook failed - status {response.status}: {text}"
                )

            # Try to parse response JSON (Stoat returns message object on 200)
            try:
                data = json.loads(text) if text else {}
                if message_id := data.get("id"):
                    self.last_message_id = message_id
                # Stoat doesn't return attachments in response like Discord,
                # so we don't update self.attachments here
            except json.JSONDecodeError:
                pass

            # Clear state if requested
            if remove_embeds:
                self.remove_embeds()
            if remove_attachments:
                self.remove_attachments()
            if clear_state:
                self.content = None
                self.masquerade = None
                self.remove_embeds()
                self.remove_attachments()

            return response
