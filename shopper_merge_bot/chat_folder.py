from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, utils
from telethon.tl import functions, types


@dataclass(frozen=True)
class ImportedSource:
    peer_id: str
    title: str
    username: str


@dataclass(frozen=True)
class FolderImportResult:
    slug: str
    title: str
    sources: tuple[ImportedSource, ...]
    joined_count: int


def extract_chat_folder_slug(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None

    if "/" not in cleaned and "?" not in cleaned:
        return cleaned

    parsed = urlparse(cleaned)
    if parsed.scheme == "tg" and parsed.netloc == "addlist":
        slug = parse_qs(parsed.query).get("slug", [""])[0]
        return slug.strip() or None

    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"t.me", "telegram.me"} and len(path_parts) >= 2:
        if path_parts[0].lower() == "addlist":
            return path_parts[1].split("?", 1)[0].strip() or None

    return None


def _peer_key(peer: object) -> tuple[str, int] | None:
    if isinstance(peer, types.PeerChannel):
        return ("channel", peer.channel_id)
    if isinstance(peer, types.PeerChat):
        return ("chat", peer.chat_id)
    if isinstance(peer, types.PeerUser):
        return ("user", peer.user_id)
    return None


def _entity_indexes(invite: object) -> tuple[dict[int, object], dict[int, object]]:
    chats = {chat.id: chat for chat in getattr(invite, "chats", [])}
    users = {user.id: user for user in getattr(invite, "users", [])}
    return chats, users


def _entity_for_peer(
    peer: object,
    chats: dict[int, object],
    users: dict[int, object],
) -> object | None:
    key = _peer_key(peer)
    if key is None:
        return None

    kind, peer_id = key
    if kind in {"channel", "chat"}:
        return chats.get(peer_id)
    return users.get(peer_id)


def _input_peer_for_peer(
    peer: object,
    chats: dict[int, object],
    users: dict[int, object],
) -> object | None:
    entity = _entity_for_peer(peer, chats, users)
    if entity is None:
        return None

    try:
        return utils.get_input_peer(entity)
    except TypeError:
        return None


def _source_from_entity(entity: object) -> ImportedSource | None:
    try:
        peer_id = str(utils.get_peer_id(entity))
    except TypeError:
        return None

    title = (
        getattr(entity, "title", None)
        or " ".join(
            part
            for part in (
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            )
            if part
        )
        or getattr(entity, "username", None)
        or peer_id
    )
    username = getattr(entity, "username", None) or ""
    return ImportedSource(peer_id=peer_id, title=str(title), username=str(username))


def _invite_title(invite: object) -> str:
    title = getattr(invite, "title", None)
    text = getattr(title, "text", None)
    if text:
        return str(text)
    return "Cartella Telegram"


async def import_chat_folder(
    client: TelegramClient,
    folder_link: str,
) -> FolderImportResult:
    slug = extract_chat_folder_slug(folder_link)
    if not slug:
        raise ValueError("Link cartella non valido. Usa un link tipo https://t.me/addlist/...")

    invite = await client(functions.chatlists.CheckChatlistInviteRequest(slug=slug))
    chats, users = _entity_indexes(invite)

    peers_to_store = []
    peers_to_join = []
    if isinstance(invite, types.chatlists.ChatlistInviteAlready):
        peers_to_store.extend(invite.already_peers)
        peers_to_store.extend(invite.missing_peers)
        peers_to_join.extend(invite.missing_peers)
    else:
        peers_to_store.extend(getattr(invite, "peers", []))
        peers_to_join.extend(getattr(invite, "peers", []))

    input_peers = []
    for peer in peers_to_join:
        if isinstance(peer, types.PeerUser):
            continue
        input_peer = _input_peer_for_peer(peer, chats, users)
        if input_peer is not None:
            input_peers.append(input_peer)

    if input_peers:
        if isinstance(invite, types.chatlists.ChatlistInviteAlready):
            await client(
                functions.chatlists.JoinChatlistUpdatesRequest(
                    chatlist=types.InputChatlistDialogFilter(invite.filter_id),
                    peers=input_peers,
                )
            )
        else:
            await client(
                functions.chatlists.JoinChatlistInviteRequest(
                    slug=slug,
                    peers=input_peers,
                )
            )

    seen = set()
    sources = []
    for peer in peers_to_store:
        entity = _entity_for_peer(peer, chats, users)
        if entity is None:
            continue
        source = _source_from_entity(entity)
        if source is None or source.peer_id in seen:
            continue
        seen.add(source.peer_id)
        sources.append(source)

    return FolderImportResult(
        slug=slug,
        title=_invite_title(invite),
        sources=tuple(sources),
        joined_count=len(input_peers),
    )
