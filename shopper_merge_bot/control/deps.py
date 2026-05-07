from __future__ import annotations

import re

from telethon import events

from shopper_merge_bot import __version__
from shopper_merge_bot.chat_folder import import_chat_folder
from shopper_merge_bot.config import parse_chat_ref
from shopper_merge_bot.constants import PRIVATE_DELETE_SCAN_LIMIT
from shopper_merge_bot.dedupe import OfferRecord
from shopper_merge_bot.media import message_ids_from_result as _message_ids_from_result
from shopper_merge_bot.menu import (
    PUBLISH_MODE_MENU_ONLY,
    grouped_active_offers,
    is_menu_only_enabled,
    open_menu_storage_key,
    parse_menu_callback_data,
    publish_mode,
    set_publish_mode,
)
from shopper_merge_bot.offer_analysis import known_filter_categories, parse_price_limit, source_score
from shopper_merge_bot.runtime import (
    command_arg,
    control_help,
    delete_messages_with_fallback,
    delete_offer_menu,
    destination_peer_id,
    edit_offer_message,
    entity_kind,
    entity_peer_id,
    entity_title,
    expand_offer_menu,
    filters_text,
    first_user_client,
    is_control_admin,
    is_control_bot_entity,
    is_private_user_destination,
    maybe_join_source,
    merge_duplicate_active_offers,
    migrate_active_posts_to_menu_only,
    purge_filtered_offers,
    purge_inactive_link_offers,
    purge_inactive_published_messages,
    purge_legacy_offers,
    purge_private_history_messages,
    purge_private_structured_offer_messages,
    purge_unmerged_destination_messages,
    recategorize_active_offers,
    reformat_active_offers,
    refresh_active_offer_gifs,
    refresh_destination,
    resolve_dialog_ref,
    save_source_entity,
    sync_offer_menus,
    unique_clients,
    upsert_menu_index,
    verify_marked_deleted_offers,
)
