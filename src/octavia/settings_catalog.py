"""Every setting her Settings window shows: where it lives, what it's called in plain words, and how it applies.

Three homes, each written where it's kept today (never ``config/default.yaml``):

* config settings (``audio.earcons``): kept in ``data/settings.json`` (``octavia.user_settings``), laid over the YAML
  files. A choice there gives way once ``config/local.yaml`` is edited for the same key.
* keys (``env:CANVAS_API_TOKEN``): lines of ``.env`` (``octavia.keys``), read through ``config.current_env``.
* her voice volume (``audio.output_volume``): ``data/voice_volume.json``, the volume "talk louder" changes.

``needs_restart`` tells the settings she only reads when she starts (a model loaded, a device opened, a feature
started or not): the window marks them and offers a restart. The rest she picks up while running
(``core/live_settings.py``).
Choices and number ranges come from the config's own types, so the window can't offer what wouldn't load.
"""

from __future__ import annotations

import enum
import types
import typing
from dataclasses import dataclass, field, replace
from typing import Any, Literal

ENV = "env:"


@dataclass(frozen=True)
class Group:
    """Settings that belong together inside a section, under a small heading."""

    title: str
    keys: tuple[str, ...]
    about: str = ""
    advanced: bool = False  # tucked away until "Fine-tuning" is opened (or a search finds one)
    # Only meaningful while another setting has this value, e.g. ("avatar.form", "character"): shown dimmed, with
    # ``about`` saying why, otherwise.
    when: tuple[str, str] | None = None


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    glyph: str  # a Segoe Fluent Icons / MDL2 Assets code point
    about: str
    area: str  # the heading it's listed under in the window's sidebar
    groups: tuple[Group, ...] = ()
    words: str = ""  # more words the search finds the whole section by


@dataclass(frozen=True)
class Setting:
    key: str  # a dotted config key, or "env:NAME" for a line of .env
    label: str
    about: str = ""
    # bool | choice | int | float | percent | text | list | time | span | hotkey | input | output | volume |
    # secret | envtext. Left empty, it's worked out from the config's type.
    kind: str = ""
    restart: bool = False  # for a key in .env: its integration is set up at startup (``keys.Key.restart``)
    choices: tuple[str, ...] = ()  # filled in from the type when empty
    names: dict[str, str] = field(default_factory=dict)  # how a choice is shown: {"safe_changes": "Safe changes"}
    unit: str = ""  # "s", "min", "ms", "h", "px", "days"...
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    words: str = ""  # more words the search finds it by
    # Where it's shown: filled in from ``SECTIONS`` by ``all_settings``, never written in an entry.
    section: str = ""
    group: str = ""
    advanced: bool = False
    when: tuple[str, str] | None = None

    @property
    def env(self) -> str | None:
        return self.key[len(ENV) :] if self.key.startswith(ENV) else None


def _g(title: str, *keys: str, about: str = "", advanced: bool = False, when: tuple[str, str] | None = None) -> Group:
    return Group(title, keys, about, advanced, when)


def _keys(*names: str) -> tuple[str, ...]:
    return tuple(ENV + name for name in names)


# The window's map: sections in the sidebar's order, under their areas, and each setting in exactly one group (a
# test holds the catalog to that).
SECTIONS: tuple[Section, ...] = (
    Section(
        "general",
        "General",
        "",
        "Who she is, who you are, and where she shows up in Windows.",
        "Her",
        (
            _g("You and her", "assistant.name", "assistant.wake_phrase", "assistant.user_name", "assistant.home"),
            _g("In Windows", "system.tray_icon", "system.explorer_menu", "system.revive"),
        ),
    ),
    Section(
        "voice",
        "Voice",
        "",
        "How she sounds (Fish Audio, or her cloned voice made on this PC), keeping up when Fish is slow, and her backup "
        "voice.",
        "Her",
        (
            _g(
                "Her voice",
                "tts.provider",
                "tts.fish_audio.reference_id",
                "tts.fish_audio.model",
                "tts.fish_audio.speed",
                "tts.fish_audio.expressive_tags",
                "tts.fish_audio.place_cues",
                "tts.rhythm",
            ),  # fmt: skip
            _g(
                "Keeping up when Fish is slow",
                "tts.fish_audio.latency",
                "tts.min_chunk_chars",
                "tts.fish_audio.chunk_length",
                about="Pauses mid-reply come from Fish's speed. Small pieces keep her talking.",
            ),
            _g(
                "Voice effect",
                "tts.voice_effect",
                about="Changes how she sounds on either voice. Off is her plain voice.",
            ),
            _g("Backup voice", "tts.fallback", "tts.windows_voice", "tts.fallback_after_s"),
            _g(
                "Fine-tuning",
                "tts.fish_audio.temperature",
                "tts.fish_audio.top_p",
                "tts.fish_audio.transport",
                "tts.fallback_cooldown_s",
                advanced=True,
            ),
        ),
        words="tts speech fish",
    ),
    Section(
        "look",
        "Look",
        "",
        "How she appears on your screen: the orb or her character, subtitles, pop-up panels and marks.",
        "Her",
        (
            _g(
                "On your screen",
                "avatar.enabled",
                "avatar.form",
                "avatar.orb_size",
                "avatar.mode",
                "avatar.hide_after_s",
                "avatar.click_to_talk",
                "avatar.point",
                "avatar.hide_from_capture",
            ),  # fmt: skip
            _g("Subtitles", "avatar.subtitles"),
            _g("Little reactions", "avatar.glance", "avatar.startle", "avatar.rainboom", "avatar.victory_lap"),
            _g(
                "Pop-up panels",
                "visuals.enabled",
                "visuals.stay_s",
                "visuals.max_panels",
                "visuals.status_panel",
                "visuals.explainer_orb_points",
                "visuals.shelf",
                "visuals.shelf_days",
                "visuals.hide_from_capture",
                "visuals.idle_quit_s",
            ),
            _g("Marks on your screen", "visuals.marks", "visuals.marks_stay_s"),
            _g("Circling with the mouse", "circling.enabled", "circling.linger_s", "circling.ring"),
            _g(
                "Pictures and pages from the web",
                "visuals.web_images",
                "visuals.image_stay_s",
                "visuals.safe_search",
                "visuals.web_pages",
                "visuals.page_stay_s",
            ),
            _g(
                "Her 3D character",
                "avatar.height",
                "avatar.framing",
                "avatar.lip_sync",
                "avatar.hover",
                "avatar.trail",
                "avatar.nap_after_min",
                "avatar.perch",
                "avatar.point_linger_s",
                about="Only for her 3D character; she's the orb now.",
                when=("avatar.form", "character"),
            ),
        ),
        words="appearance avatar orb character theme visual",
    ),
    Section(
        "sound",
        "Sound",
        "",
        "Microphone, speaker and the little sounds she makes.",
        "Her",
        (
            _g("Devices", "audio.input_device", "audio.output_device", "audio.headphones"),
            _g("Loudness", "audio.output_volume", "audio.earcon_volume"),
            _g("Direction", "audio.directional", "audio.directional_width"),
            _g("Her little sounds", "audio.earcons", "audio.sound_pack", "audio.working_sound", "audio.done_sound"),
        ),
    ),
    Section(
        "conversation",
        "Conversation",
        "",
        "How a conversation with her flows, and what she does while she works.",
        "Talking",
        (
            _g(
                "Flow",
                "conversation.follow_up_window_s",
                "conversation.name_on_calls",
                "conversation.name_while_playing",
                "conversation.session_timeout_s",
                "conversation.confirm_listen_s",
                "conversation.turn_stall_s",
            ),  # fmt: skip
            _g(
                "While she works",
                "conversation.acknowledge",
                "conversation.wake_ack",
                "conversation.narrate_work",
                "conversation.backchannel",
                "conversation.quick_replies",
            ),  # fmt: skip
            _g(
                "Answering sooner",
                "conversation.acknowledge_early_ms",
                "conversation.early_start",
                "conversation.opener",
                "conversation.opener_wait_ms",
                "conversation.max_context_tokens",
            ),  # fmt: skip
        ),
    ),
    Section(
        "listening",
        "Listening",
        "",
        "Her wake word, when you've finished talking, and interrupting her.",
        "Talking",
        (
            _g(
                "Wake word",
                "wake.sherpa_kws.threshold",
                "wake.sherpa_kws.boost",
                "wake.sherpa_kws.extra_spellings",
                "wake.second_chance",
                "wake.second_chance_max_s",
                "wake.cooldown_s",
            ),  # fmt: skip
            _g(
                "When you've finished",
                "vad.end_silence_ms",
                "vad.no_speech_timeout_s",
                "vad.max_utterance_s",
                "vad.early_transcript_ms",
            ),  # fmt: skip
            _g(
                "Interrupting her",
                "barge_in.enabled",
                "barge_in.wake_phrase",
                "barge_in.voice",
                "barge_in.echo_cancellation",
            ),  # fmt: skip
            _g(
                "Fine-tuning",
                "vad.threshold",
                "vad.min_speech_ms",
                "vad.pre_roll_ms",
                "barge_in.vad_threshold",
                "barge_in.min_speech_ms",
                "barge_in.aec_warmup_s",
                "stt.num_threads",
                advanced=True,
            ),  # fmt: skip
        ),
        words="microphone hearing vad",
    ),
    Section(
        "shortcuts",
        "Shortcuts",
        "",
        "Work anywhere in Windows. Click one, then press the keys you want; Backspace turns it off.",
        "Talking",
        (_g("Keys", "hotkeys.enabled", "hotkeys.talk", "hotkeys.stop", "hotkeys.mute", "hotkeys.chat"),),
        words="hotkeys keyboard",
    ),
    Section(
        "quiet",
        "Quiet hours",
        "",
        "When she keeps to herself: snooze her now, or set the hours she stays quiet every day.",
        "Talking",
        (
            _g("Every day", "futures.quiet_hours", "scheduler.quiet_hours"),
            _g("How often she speaks up", "futures.max_per_hour", "futures.max_per_day"),
        ),
        words="dnd do not disturb snooze silence night sleep focus",
    ),
    Section(
        "brain",
        "Brain",
        "",
        "Who answers, the Claude models that think for her, her quick answers, and hard questions.",
        "Thinking",
        (
            _g("Who answers", "brain.answers"),
            _g(
                "Claude",
                "brain.claude.model",
                "brain.claude.fallback_model",
                "brain.claude.effort",
                "brain.claude.max_turns",
            ),  # fmt: skip
            _g("Quick answers", "fast_chat.enabled", "fast_chat.model", "fast_chat.max_words", "routing.fast_path"),
            _g(
                "Hard questions",
                "deep_think.enabled",
                "deep_think.effort",
                "deep_think.max_per_day",
                "deep_think.max_second_opinions",
            ),
            _g("Learning from her tasks", "playbooks.enabled", "skills.enabled", "skills.notice_repeats"),
            _g(
                "Fine-tuning",
                "fast_chat.context_turns",
                "deep_think.model",
                "deep_think.time_limit_min",
                "deep_think.web",
                "fast_chat.timeout_s",
                "playbooks.model",
                "playbooks.effort",
                "playbooks.min_steps",
                "skills.min_tasks",
                "skills.min_steps",
                "claims.mode",
                "brain.claude.warm_cache",
                "brain.claude.resume_last_session",
                "brain.claude.connect_timeout_s",
                "brain.claude.stall_timeout_s",
                advanced=True,
            ),  # fmt: skip
        ),
        words="claude model ai llm opus sonnet haiku",
    ),
    Section(
        "local",
        "Local brain",
        "",
        "A small model on your GPU beside Claude. In shadow it only takes notes; routed, it answers simple "
        "commands itself and covers for Claude when it's down.",
        "Thinking",
        (
            _g(
                "The model",
                "local_brain.enabled",
                "local_brain.mode",
                "local_brain.model",
                "local_brain.server",
                "local_brain.device",
                "local_brain.context",
            ),  # fmt: skip
            _g(
                "What it answers",
                "local_brain.route_max_words",
                "local_brain.route_min_confidence",
                "local_brain.uncensored",
                "local_brain.answer_timeout_s",
                "local_brain.history_turns",
            ),  # fmt: skip
            _g("When Claude is down", "local_brain.fallback_timeout_s", "local_brain.claude_retry_s"),
            _g("While you game", "local_brain.game_unload_s", "local_brain.game_reload_s"),
            _g(
                "Was that for her?",
                "addressee.enabled",
                "addressee.threshold",
                "addressee.repeat_window_s",
                about="In shadow: it judges what you say near the PC without her name, and only takes notes.",
            ),
            _g(
                "Fine-tuning",
                "local_brain.route_tools",
                "local_brain.port",
                "local_brain.slot_cache_dir",
                advanced=True,
            ),  # fmt: skip
        ),
        words="qwen llama gpu llm offline local model shadow route",
    ),
    Section(
        "usage",
        "Usage",
        "",
        "How much of your Claude limit her own background work may take. Your conversations never wait on it.",
        "Thinking",
        (
            _g(
                "Budget",
                "usage_budget.enabled",
                "usage_budget.daily_share",
                "usage_budget.max_five_hour",
                "usage_budget.max_weekly",
            ),  # fmt: skip
            _g(
                "What uses it",
                "self_improvement.max_builds_per_day",
                "self_improvement.parallel_builds",
                "self_improvement.nightly_builds",
                "self_improvement.frontier_weekly",
                "objectives.respect_limits",
                "objectives.max_open",
            ),  # fmt: skip
        ),
        words="limit budget subscription rate quota tokens cost",
    ),
    Section(
        "permissions",
        "Permissions",
        "",
        "What she may do without asking first.",
        "Trust",
        (
            _g("Asking first", "permissions.safe", "permissions.confirm", "permissions.confirm_timeout_s"),
            _g(
                "Sending and submitting",
                "permissions.submit_without_asking",
                "permissions.always_ask_before_sending",
                "permissions.trusted_apps",
            ),  # fmt: skip
        ),
    ),
    Section(
        "privacy",
        "Privacy & memory",
        "",
        "What she remembers about you, your screen and your day.",
        "Trust",
        (
            _g("Memory", "memory.enabled", "memory.max_prompt_items"),
            _g(
                "Screen history",
                "screen_history.enabled",
                "screen_history.retention_days",
                "screen_history.exclude_apps",
                "screen_history.exclude_sites",
                "screen_history.ocr",
            ),  # fmt: skip
            _g(
                "What she learns",
                "futures.keep_request_words",
                "futures.off_limits",
                "futures.retention_days",
                "futures.week_free_hours_weekday",
                "futures.week_free_hours_weekend",
            ),
            _g(
                "Money",
                "finance.enabled",
                "finance.heads_ups",
                "finance.reads_per_day",
                "finance.low_balance",
                "finance.inbox",
                about="Read-only, on this PC. She never moves money.",
            ),
            _g("Your tone", "emotion.enabled", "emotion.note"),
            _g(
                "Fine-tuning",
                "screen_history.idle_after_s",
                "screen_history.max_cpu_percent",
                "screen_history.max_mb",
                "emotion.min_confidence",
                "emotion.prosody",
                advanced=True,
            ),  # fmt: skip
        ),
        words="recall",
    ),
    Section(
        "proactive",
        "Proactive help",
        "",
        "Speaking up on her own: heads-ups, offers, routines and study nudges.",
        "Her day",
        (
            _g(
                "Heads-ups",
                "futures.enabled",
                "futures.proactive",
                "futures.offers",
                "futures.offers_per_day",
                "futures.experiments",
            ),  # fmt: skip
            _g(
                "What she watches",
                "futures.context_watch",
                "futures.canvas_watch",
                "futures.calendar_watch",
                "heads_up.enabled",
                "deadlines.enabled",
                "unprompted.enabled",
            ),  # fmt: skip
            _g("Routines", "routines.enabled", "routines.offers"),
            _g(
                "Work before you ask",
                "proactive.level",
                "proactive.max_items",
                "proactive.night",
                "proactive.projects",
                about="At night, while you're away, she prepares a few things and hands them over in the morning.",
            ),  # fmt: skip
            _g(
                "Studying",
                "study.enabled",
                "study.nudges",
                "study.exam_days",
                "study.canvas_exams",
                "study.min_reviews",
            ),  # fmt: skip
            _g(
                "Fine-tuning",
                "futures.cooldown_h",
                "futures.heads_up_h",
                "futures.canvas_every_min",
                "heads_up.model",
                "heads_up.settle_s",
                "unprompted.model",
                "futures.offer_assignment_h",
                "futures.offer_meeting_min",
                "deadlines.canvas_h",
                "deadlines.event_min",
                "futures.work_apps",
                "routines.mine_at",
                "routines.history_days",
                "routines.min_days",
                "routines.offers_per_day",
                "routines.exclude_apps",
                "proactive.away_min",
                "proactive.look_ahead_days",
                "proactive.rank",
                "proactive.rank_model",
                "proactive.rank_effort",
                "proactive.steer_min_decisions",
                "proactive.steer_agreement",
                advanced=True,
            ),  # fmt: skip
        ),
        words="futures remarks",
    ),
    Section(
        "tasks",
        "Tasks & schedule",
        "",
        "TickTick, reminders and the jobs she runs on a schedule.",
        "Her day",
        (
            _g(
                "TickTick",
                "ticktick.changes",
                "ticktick.deletes",
                "ticktick.reminders",
                "ticktick.remind_before_min",
                "ticktick.morning_summary",
            ),  # fmt: skip
            _g("Scheduled jobs", "scheduler.enabled", "scheduler.changes"),
            _g("Fine-tuning", "ticktick.check_every_min", advanced=True),
        ),
    ),
    Section(
        "inner",
        "Inner life",
        "",
        "Her nightly reflection, journal and mood.",
        "Her day",
        (
            _g(
                "Reflection",
                "inner_life.enabled",
                "inner_life.reflect_at",
                "inner_life.bring_up",
                "inner_life.mood",
                "inner_life.self_notes",
                "inner_life.looking_back",
                "inner_life.dreams",
                "inner_life.web",
            ),  # fmt: skip
            _g(
                "Fine-tuning",
                "inner_life.max_per_day",
                "inner_life.idle_minutes",
                "inner_life.time_limit_min",
                advanced=True,
            ),  # fmt: skip
        ),
    ),
    Section(
        "evolve",
        "Self-improvement",
        "",
        "Her fixing and extending her own code, and objectives you hand her.",
        "Upkeep",
        (
            _g(
                "Building",
                "self_improvement.enabled",
                "self_improvement.auto_build",
                "self_improvement.auto_install",
                "self_improvement.builder_model",
                "self_improvement.builder_effort",
                "self_improvement.announce",
            ),  # fmt: skip
            _g("Night shift", "self_improvement.nightly_at", "self_improvement.morning_report"),
            _g(
                "Learning from you",
                "self_scoring.enabled",
                "self_scoring.experiments",
                "self_scoring.keep_habits",
                "self_scoring.lessons",
                "self_scoring.strategies",
            ),  # fmt: skip
            _g("Objectives", "objectives.enabled", "objectives.project_roots", "objectives.web"),
            _g(
                "Fine-tuning",
                "self_improvement.build_timeout_min",
                "self_improvement.retry_failed_checks",
                "self_improvement.notice_errors",
                "objectives.model",
                "objectives.effort",
                "objectives.review_model",
                "objectives.review_effort",
                "objectives.research_parallel",
                "objectives.code_timeout_min",
                "objectives.check_timeout_min",
                advanced=True,
            ),  # fmt: skip
        ),
        words="evolve builds updates",
    ),
    Section(
        "system",
        "System",
        "",
        "Updates to her code, her logs, and how she runs.",
        "Upkeep",
        (
            _g("New code", "system.restart_for_new_code", "system.new_code_quiet_s"),
            _g("Logs", "logging.level", "logging.show_replies"),
            _g(
                "Fine-tuning",
                "tools.search_engine",
                "logging.console",
                "system.disable_power_throttling",
                advanced=True,
            ),  # fmt: skip
        ),
    ),
    Section(
        "phone",
        "Phone & Discord",
        "",
        "Texting and calling her through your Discord bot.",
        "Connections",
        (
            _g(
                "Texting",
                "discord_bridge.enabled",
                "discord_bridge.owner_id",
                "discord_bridge.notify",
                "discord_bridge.away_after_min",
                "discord_bridge.speak_replies",
            ),  # fmt: skip
            _g(
                "Voice calls",
                "discord_bridge.voice_calls",
                "discord_bridge.mute_pc_on_call",
                "discord_bridge.call_wake_word",
            ),  # fmt: skip
            _g(
                "Fine-tuning", "discord_bridge.confirm_timeout_s", "discord_bridge.max_turns_per_10_min", advanced=True
            ),  # fmt: skip
        ),
    ),
    Section(
        "maps",
        "Maps",
        "",
        "God's Eye View, the 3D globe she shows places on.",
        "Connections",
        (_g("God's Eye View", "gods_eye.enabled", "gods_eye.folder", "gods_eye.address"),),
    ),
    Section(
        "keys",
        "Accounts & keys",
        "",
        "API keys and accounts, kept in .env on this PC.",
        "Connections",
        (
            _g(
                "Her brain and her voice",
                *_keys("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FISH_AUDIO_API_KEY", "FISH_AUDIO_VOICE_ID"),
            ),
            _g("School and tasks", *_keys("CANVAS_API_TOKEN", "CANVAS_BASE_URL", "TICKTICK_API_TOKEN")),
            _g("Mail and calendar", *_keys("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "CALENDAR_ICS_URLS")),
            _g("Music", *_keys("SPOTIFY_CLIENT_ID", "AUDD_API_TOKEN")),
            _g("Discord", *_keys("DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID")),
            _g("Maps", *_keys("CESIUM_ION_TOKEN", "GOOGLE_MAPS_API_KEY")),
        ),
        words="api token password credentials env",
    ),
)

# Config settings the window leaves to the config files, on purpose: where her files and models live, what's wired
# at build time (tools, plugins, props), log paths, and the switches that would loosen her safety rules (tool
# permissions, unrestricted built-in tools). ``test_every_config_setting_is_shown_or_left_out_on_purpose`` makes a
# new setting land in a section or here.
# hive: the trust path, set by the build and `octavia welcome`. brain.openai_compatible: another model's server, key
# and name, set with brain.provider (not shown either) in config/local.yaml or by the setup wizard's brain step.
NOT_SHOWN_PREFIXES = (
    "avatar.props.",
    "tools.",
    "permissions.",
    "hive.",
    "brain.openai_compatible.",
)
NOT_SHOWN = frozenset(
    {
        # files and folders
        "assistant.personality_file", "assistant.system_prompt_file", "assistant.acknowledgments_file",
        "assistant.quick_replies_file", "assistant.home_latitude", "assistant.home_longitude",
        "wake.sherpa_kws.model_dir", "wake.openwakeword.model_path", "vad.model_path",
        "vad.smart_turn_model", "stt.model_dir", "stt.hf_repo", "memory.database", "avatar.model", "avatar.credit",
        "avatar.electron", "avatar.props_dir", "futures.database", "screen_history.database", "inner_life.database",
        "routines.database", "logging.file", "routing.log_file", "local_brain.log_file", "local_brain.route_log_file",
        "claims.log_file", "claims.promises_file", "proactive.folder", "addressee.log_file", "emotion.model_dir",
        "brain.claude.cli_path", "brain.claude.config_dir", "brain.claude.work_dir",
        # which engine or model, changed together with files the setup puts in place
        "audio.host_api", "wake.engine", "wake.openwakeword.threshold", "wake.sherpa_kws.num_threads", "stt.engine", "stt.model",
        "stt.quantization", "stt.language", "brain.provider", "brain.claude.auth", "tts.local.python",
        "tts.local.engine_dir", "tts.local.model_dir", "tts.local.voice", "tts.local.voice_text", "tts.local.gpu",
        "tts.local.first_frames", "tts.local.temperature", "tts.local.detail_temperature", "tts.local.min_p", "tts.local.seed", "tts.local.language", "tts.local.start_timeout_s",
        "tts.local.game_unload_s", "tts.local.game_reload_s",
        "tts.fish_audio.api_key_env", "tts.fish_audio.reference_id_env", "tts.fish_audio.base_url",
        "tts.fish_audio.sample_rate", "emotion.num_threads",
        # how she's built and checked: tool loading, her builder's limits and protected paths
        "brain.claude.builtin_tools", "brain.claude.allow_unrestricted_builtins", "brain.claude.tools_on_demand",
        "brain.claude.always_loaded_tools", "self_improvement.builder_max_turns", "self_improvement.test_timeout_min",
        "self_improvement.extra_protected_paths", "objectives.checks", "system.single_instance",
        # inner timings nobody tunes by feel
        "vad.neg_threshold", "tts.fish_audio.spare_socket_max_idle_s", "avatar.point_height",
        "scheduler.check_every_s", "futures.exclude_sources", "screen_history.poll_s", "screen_history.dwell_s",
        "screen_history.recapture_s", "screen_history.ocr_every_s", "inner_life.catch_up_hours",
        "inner_life.digest_chars", "routines.min_support", "routing.log", "emotion.max_wait_ms",
        "emotion.max_audio_s", "discord_bridge.max_file_mb", "discord_bridge.max_attachment_mb",
    }
)  # fmt: skip

_ASK = {"allow": "Just do it", "auto": "Auto (ask only on her own)", "confirm": "Ask first", "deny": "Never"}
_EFFORT = {"low": "Low (fastest)", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max"}


def _s(*args: Any, **kwargs: Any) -> Setting:
    return Setting(*args, **kwargs)


SETTINGS: tuple[Setting, ...] = (
    # ---- General
    _s("assistant.name", "Her name", "What she's called on screen and in her own words."),
    _s(
        "assistant.wake_phrase",
        "Wake word",
        "What you say to call her, e.g. \u201coctavia\u201d.",
        words="hotword wakeword",
    ),  # fmt: skip
    _s("assistant.user_name", "What she calls you", "Empty: \u201cthe user\u201d."),
    _s(
        "assistant.home",
        "Home",
        "Your town, e.g. \u201cPortland, Oregon\u201d: the weather, directions and maps use it when you name no "
        "place. Empty: she asks.",
        words="city location town weather",
    ),
    # ---- Conversation
    _s(
        "conversation.follow_up_window_s",
        "Follow-up window",
        "After she answers, keep listening this long for a follow-up without her name.",
        unit="s",
        hi=60,
    ),  # fmt: skip
    _s(
        "conversation.name_on_calls",
        "Need her name on calls",
        "On a call in Discord, Zoom, Teams or a browser, no follow-up without saying her name.",
    ),  # fmt: skip
    _s(
        "conversation.name_while_playing",
        "Need her name while something plays",
        "While a video, music or a game plays, no follow-up without her name, so she doesn't answer what's playing.",
    ),  # fmt: skip
    _s(
        "conversation.session_timeout_s",
        "Fresh start after",
        "After this much silence, the next conversation starts with a clean slate.",
        unit="s",
    ),  # fmt: skip
    _s(
        "conversation.max_context_tokens",
        "Keep her context short",
        "Past this many tokens, her brain starts afresh between turns and picks up the thread from the transcript. "
        "She answers just as fast up to about 90,000; past 120,000 she slows down. Starting afresh costs a few "
        "seconds each time, so she always leaves a conversation 20,000 of room above her own prompt. 0: never.",
        unit="tokens",
        step=5000,
        hi=200000,
        words="context length tokens fast slow",
    ),  # fmt: skip
    _s(
        "conversation.wake_ack",
        "When she wakes",
        "A chime when she hears her name, or nothing.",
        names={"chime": "Chime", "none": "Nothing"},
    ),  # fmt: skip
    _s(
        "conversation.acknowledge",
        "While she thinks",
        "A quick \u201cOn it!\u201d in her voice, a soft chime, or nothing.",
        names={"speech": "Say \u201cOn it!\u201d", "chime": "Chime", "none": "Nothing"},
    ),  # fmt: skip
    _s(
        "conversation.quick_replies",
        "Quick replies",
        "Answer \u201cthanks\u201d and the like instantly, without using Claude.",
    ),  # fmt: skip
    _s(
        "conversation.confirm_listen_s",
        "Wait for yes or no",
        "How long she listens for your answer when she asks before doing something.",
        unit="s",
    ),  # fmt: skip
    _s(
        "conversation.narrate_work",
        "Talk me through it",
        "Say what she's doing between steps. Off: work quietly and say only the result.",
    ),  # fmt: skip
    _s(
        "conversation.backchannel",
        "\u201cMhm\u201d when you pause",
        "A soft sound when you stop mid-thought, so you know she's still listening.",
    ),  # fmt: skip
    _s(
        "conversation.acknowledge_early_ms",
        "Early \u201cOn it!\u201d",
        "With headphones, acknowledge this soon into the pause after a finished request. 0: off.",
        unit="ms",
    ),  # fmt: skip
    _s(
        "conversation.early_start",
        "Start thinking in your pause",
        "With the early \u201cOn it!\u201d, Claude starts on the request too. It acts only once you've finished.",
    ),  # fmt: skip
    _s(
        "conversation.opener",
        "Opening words",
        "A first phrase about what you said, in place of “On it!”, while Claude works out the rest. The "
        "phrase takes about a second to come, so with it on her first sound is later, not sooner. Shadow: only "
        "logged, never said.",
        kind="choice",
        choices=("off", "shadow", "on"),
        names={"off": "Off", "shadow": "Shadow (log only)", "on": "On"},
        words="opener reflex first words haiku dual",
    ),  # fmt: skip
    _s(
        "conversation.opener_wait_ms",
        "Wait for opening words",
        "With opening words on, how long the early “On it!” may wait for a phrase that could replace it. The phrase "
        "usually needs 900 ms; when it doesn't come in time, “On it!” is this much later.",
        unit="ms",
    ),  # fmt: skip
    _s(
        "conversation.turn_stall_s",
        "Give up on a stuck request after",
        "A request that gets nowhere for this long is stopped, and she tells you. 0: never.",
        unit="s",
    ),  # fmt: skip
    # ---- Sound
    _s("audio.input_device", "Microphone", "The mic she listens with.", kind="input"),
    _s("audio.output_device", "Speaker", "Where her voice plays.", kind="output"),
    _s(
        "audio.output_volume",
        "Voice volume",
        "How loud her voice is, on top of Windows' volume (\u201ctalk louder\u201d changes it too).",
        kind="volume",
        words="loud quiet",
    ),  # fmt: skip
    _s(
        "audio.headphones",
        "I use headphones",
        "Lets you interrupt her just by talking, since her voice can't leak into the mic.",
    ),  # fmt: skip
    _s(
        "audio.directional",
        "Voice from where she is",
        "Her voice and sounds come from her side of the screen, and move with her. Best with headphones.",
        words="directional spatial stereo pan left right 3d",
    ),  # fmt: skip
    _s(
        "audio.directional_width",
        "How far to the side",
        "How much quieter the far ear is when she's at the screen's edge.",
        kind="percent",
        words="directional spatial stereo pan width",
    ),  # fmt: skip
    _s("audio.earcons", "Sound cues", "The little sounds for what she's doing.", words="earcons chimes"),
    _s("audio.earcon_volume", "Sound cue volume", "How loud those little sounds are.", kind="percent"),
    _s(
        "audio.sound_pack",
        "Sound pack",
        "Ether: soft glass chimes in a long hall, like an old console's menu. Falcon: sleek and quick. "
        "Classic: her first chimes.",
        names={"ether": "Ether", "falcon": "Falcon", "classic": "Classic"},
    ),  # fmt: skip
    _s("audio.working_sound", "Thinking music", "A soft loop while she works on your request."),
    _s("audio.done_sound", "Done sound", "A rising arpeggio at the end of a request that took several steps."),
    # ---- Listening
    _s(
        "wake.second_chance",
        "Second chance",
        "When the wake word is missed, check whether what you said starts with her name.",
    ),  # fmt: skip
    _s(
        "wake.second_chance_max_s",
        "Second chance: longest speech",
        "Longer stretches of speech are someone talking, not calling her.",
        unit="s",
    ),  # fmt: skip
    _s(
        "wake.sherpa_kws.threshold",
        "Wake word strictness",
        "Higher: fewer accidental wakes, but more misses.",
        step=0.005,
    ),  # fmt: skip
    _s("wake.sherpa_kws.boost", "Wake word boost", "Higher: easier to trigger.", step=0.1),  # fmt: skip
    _s(
        "wake.sherpa_kws.extra_spellings",
        "Other spellings of her name",
        "If an unusual name is missed, e.g. xavier, octavio. Separate with commas.",
    ),  # fmt: skip
    _s("wake.cooldown_s", "Wake cooldown", "Ignore her name this long after she wakes.", unit="s"),
    _s(
        "vad.end_silence_ms",
        "Pause that ends your sentence",
        "Raise it if she cuts you off mid-thought.",
        unit="ms",
        hi=5000,
        words="silence cut off",
    ),  # fmt: skip
    _s(
        "vad.no_speech_timeout_s",
        "Give up listening after",
        "Stop listening if you don't start talking within this long.",
        unit="s",
    ),  # fmt: skip
    _s("vad.max_utterance_s", "Longest request", "The most she'll listen to in one go.", unit="s"),
    _s(
        "vad.early_transcript_ms",
        "Early transcript",
        "Start transcribing this far into a pause, so she answers sooner. 0: wait for the end (and no \u201cmhm\u201d "
        "or early \u201cOn it!\u201d).",
        unit="ms",
    ),  # fmt: skip
    _s("barge_in.enabled", "Let me interrupt her", "Stop her mid-sentence.", words="barge"),
    _s("barge_in.wake_phrase", "Interrupt with her name", "Saying her name while she talks stops her."),
    _s(
        "barge_in.voice",
        "Interrupt by just talking",
        "Automatic: on with headphones; on speakers when echo cancellation works well.",
        kind="choice",
        choices=("auto", "on", "off"),
        names={"auto": "Automatic", "on": "On", "off": "Off"},
    ),  # fmt: skip
    _s(
        "barge_in.echo_cancellation",
        "Echo cancellation",
        "Remove her own voice from the mic.",
        words="aec",
    ),  # fmt: skip
    # ---- Voice
    _s(
        "tts.provider",
        "Voice engine",
        "Automatic: her cloned voice made on your GPU (starts in about 0.4 s), with Fish Audio as its backup. "
        "Local only: that voice, never Fish. Fish Audio only: her voice from the cloud. Switches at once when she "
        "started on her local voice; “use Fish” or “back to normal” does it by voice.",
        kind="choice",
        choices=("local", "local_only", "fish_audio"),
        names={
            "local": "Automatic (local, Fish as backup)",
            "local_only": "Local only",
            "fish_audio": "Fish Audio only",
        },
        words="local qwen clone cloned engine fish speed only automatic",
    ),  # fmt: skip
    _s(
        "brain.answers",
        "Who answers",
        "Automatic: her local brain takes simple commands (as Local brain says) and Claude the rest. Claude only: "
        "the local brain never answers. Local brain only: it answers everything you say and tells you plainly what "
        "it can't do; her background work still uses Claude. “Only use Claude”, “only use your local "
        "brain” and “back to normal” do it by voice.",
        kind="choice",
        names={"auto": "Automatic", "claude": "Claude only", "local": "Local brain only"},
        words="routing router local cloud claude only offline brain model",
    ),
    _s(
        "tts.fish_audio.reference_id",
        "Voice ID",
        "Which Fish Audio voice she speaks with (32 characters). Empty: the one in .env.",
        words="fish",
    ),  # fmt: skip
    _s(
        "tts.fish_audio.model",
        "Voice model",
        "s2.1-pro-free is free (fair use); s2.1-pro is paid.",
        kind="choice",
        choices=("s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"),
    ),  # fmt: skip
    _s(
        "tts.fish_audio.latency",
        "Latency mode",
        "Low and Balanced start speaking soonest (0.6 to 1 s). Normal waits for most of the sentence first: 2 to "
        "3 s, and her audio then only just keeps up with her talking.",
        names={"low": "Low", "balanced": "Balanced", "normal": "Normal"},
    ),  # fmt: skip
    _s("tts.fish_audio.speed", "Speaking speed", "1.0 is her natural pace.", step=0.01),
    _s(
        "tts.voice_effect",
        "Voice effect",
        "Radio: a narrow old receiver. Console: a ship's computer, doubled with a little room. Robot: a faint "
        "metallic buzz. Room: a soft small hall. Deeper: a touch lower and slower, like a tape. From her next reply; "
        "\u201cuse your radio voice\u201d or \u201cturn off the voice effect\u201d does it by voice.",
        kind="choice",
        names={
            "off": "Off",
            "radio": "Radio",
            "console": "Console",
            "robot": "Robot",
            "room": "Room",
            "deeper": "Deeper",
        },
        words="filter effect radio robot reverb echo console computer deep pitch sound fx",
    ),
    _s(
        "tts.min_chunk_chars",
        "Text sent per piece",
        "Text sent to Fish in one go. Keep it near 40: long pieces make her pause mid-reply when Fish is slow.",
        unit="chars",
        words="chunk pauses gaps stutter piece size",
    ),  # fmt: skip
    _s(
        "tts.fish_audio.chunk_length",
        "Fish chunk length",
        "How Fish splits a piece inside (100 to 300). It barely changes pauses; piece size is what matters.",
        words="chunk",
    ),  # fmt: skip
    _s(
        "tts.fish_audio.temperature",
        "Expressiveness",
        "Higher: more varied delivery. Empty: Fish's default.",
        step=0.05,
    ),  # fmt: skip
    _s(
        "tts.fish_audio.top_p",
        "Word-sound variety",
        "Fish's sampling breadth (top-p). Empty: Fish's default.",
        step=0.05,
        words="top p",
    ),
    _s("tts.fish_audio.expressive_tags", "Tone cues", "Let her use cues like [excited] in her voice."),
    _s(
        "tts.rhythm",
        "Natural rhythm",
        "Pauses that change with how a sentence ends (longer after a question or a full stop, barely one at a "
        "comma) and a pace for each sentence (a short line a little quicker, a stressed one a little slower). "
        "Never before her first words, and never longer when the next sentence is already late.",
    ),
    _s(
        "tts.fish_audio.place_cues",
        "Cues placed for her",
        "A quick Haiku session adds tone cues, laughs, breaths, stressed words and beats to each sentence after her "
        "first, the words unchanged, while the sentence before plays. It never makes her wait: a placement that's late "
        "is skipped. About three small Haiku calls a spoken reply.",
    ),
    _s(
        "tts.fallback",
        "Backup voice",
        "When Fish Audio fails: a built-in Windows voice, or silence.",
        names={"windows": "Windows voice", "none": "Silence"},
    ),  # fmt: skip
    _s("tts.windows_voice", "Windows voice", "Part of a voice's name, e.g. Zira. Empty: the first female one."),
    _s(
        "tts.fallback_after_s",
        "Switch to backup after",
        "No sound from Fish this long into a sentence: the backup says it.",
        unit="s",
    ),  # fmt: skip
    # ---- Brain
    _s(
        "brain.claude.model",
        "Main model",
        "opus, sonnet or haiku, or a full model ID.",
        kind="choice",
        choices=("opus", "sonnet", "haiku"),
    ),  # fmt: skip
    _s(
        "brain.claude.fallback_model",
        "Fallback model",
        "Used when the main model is busy.",
        kind="choice",
        choices=("sonnet", "haiku", "opus"),
    ),  # fmt: skip
    _s("brain.claude.effort", "Thinking effort", "Lower answers faster.", names=_EFFORT),
    _s(
        "brain.claude.max_turns",
        "Steps per request",
        "Tool steps she may take for one request; at the limit she pauses and asks.",
        hi=500,
    ),  # fmt: skip
    _s(
        "fast_chat.enabled",
        "Quick voice for small talk",
        "A fast, warm Claude answers chit-chat in about half a second.",
    ),  # fmt: skip
    _s(
        "fast_chat.model",
        "Quick voice model",
        "On your Claude subscription.",
        kind="choice",
        choices=("haiku", "sonnet", "opus"),
    ),  # fmt: skip
    _s(
        "fast_chat.max_words",
        "Quick voice: longest line",
        "Anything longer goes straight to the main brain.",
        unit="words",
    ),  # fmt: skip
    _s(
        "routing.fast_path",
        "Instant commands",
        "Timers, volume, play/pause and the time without calling Claude.",
        names={"on": "On", "shadow": "Log only", "off": "Off"},
    ),  # fmt: skip
    # ---- Permissions
    _s("permissions.safe", "Safe actions", "Things that can't hurt, like reading the time.", names=_ASK),
    _s(
        "permissions.confirm",
        "Risky actions",
        "Things worth a second look. Auto: no question inside your own request.",
        names=_ASK,
    ),  # fmt: skip
    _s(
        "permissions.confirm_timeout_s",
        "Question times out after",
        "No answer by then counts as no.",
        unit="s",
    ),  # fmt: skip
    _s(
        "permissions.submit_without_asking",
        "Submit without asking on",
        "Sites where she may submit and send without asking (a word of the window's title). Separate with commas.",
    ),  # fmt: skip
    _s(
        "permissions.always_ask_before_sending",
        "Always ask before sending on",
        "Sending or submitting there is asked every time, whatever you said.",
        words="email",
    ),  # fmt: skip
    _s(
        "permissions.trusted_apps",
        "Trusted apps",
        "Deleting inside your document there (a shape, a cell) needn't ask, e.g. PowerPoint.",
    ),  # fmt: skip
    # ---- Shortcuts
    _s("hotkeys.enabled", "Keyboard shortcuts", "Turn all of them on or off."),
    _s("hotkeys.talk", "Talk", "Start listening now, or interrupt and listen.", kind="hotkey"),
    _s("hotkeys.stop", "Stop", "Stop her talking.", kind="hotkey"),
    _s("hotkeys.mute", "Mute listening", "Mute or unmute her microphone.", kind="hotkey"),
    _s("hotkeys.chat", "Chat window", "Open the window to type to her.", kind="hotkey"),
    # ---- Character
    _s(
        "avatar.enabled",
        "Show her on the desktop",
        "The orb or her 3D character, in a window of her own.",
        words="avatar",
    ),
    _s(
        "avatar.mode",
        "When she's on screen",
        "Pops in when you talk and leaves when idle, or always stays.",
        names={"conversation": "During conversations", "always": "Always"},
    ),  # fmt: skip
    _s(
        "avatar.hide_after_s",
        "Leaves after",
        "How long she stays once a conversation goes quiet.",
        unit="s",
    ),  # fmt: skip
    _s(
        "avatar.form",
        "Her form",
        "Her 3D character, or a glowing orb that needs no model.",
        names={"character": "Character", "orb": "Orb"},
        words="orb avatar shape",
    ),  # fmt: skip
    _s("avatar.orb_size", "Orb size", "The orb's window, in either direction.", unit="px", lo=96, hi=600),
    _s(
        "avatar.subtitles",
        "Subtitles",
        "Show what she says beside her as she speaks, with math and code written out.",
        words="captions subtitles transcript read text",
    ),  # fmt: skip
    _s("avatar.height", "Character size", "Her window's height.", unit="px", lo=200, hi=1600),
    _s(
        "avatar.framing",
        "Framing",
        "Whole body or waist up.",
        names={"full": "Whole body", "upper": "Waist up"},
    ),  # fmt: skip
    _s("avatar.lip_sync", "Lip sync", "Move her lips with her voice."),
    _s("avatar.click_to_talk", "Click to talk", "Click her to start talking."),
    _s(
        "avatar.hide_from_capture",
        "Hide from screenshots",
        "Keep her out of screenshots, recordings and screen shares.",
    ),  # fmt: skip
    _s("avatar.point", "Point at things", "Fly over to things on your screen and point."),
    _s("avatar.trail", "Rainbow trail", "A rainbow streak when she flies."),
    _s("avatar.hover", "Hover while talking", "Float while you talk to her."),
    _s(
        "avatar.nap_after_min",
        "Nap after",
        "In always mode: idle this long on screen, she naps on a cloud. 0: never.",
        unit="min",
    ),  # fmt: skip
    _s("avatar.rainboom", "Sonic rainboom", "When something big is done."),
    _s("avatar.victory_lap", "Victory lap", "When you tick off the last task due today."),
    _s("avatar.glance", "Glance at windows", "She glances at the window you switch to."),
    _s(
        "avatar.perch",
        "Perch during builds",
        "In always mode: sit on top of your window while she builds.",
    ),  # fmt: skip
    _s("avatar.startle", "Startle", "Jump at a sudden bang or a window popping up."),
    # ---- Proactive help
    _s(
        "futures.enabled",
        "Learn from what happens",
        "Her model of your world: events, outcomes and predictions, all on this PC.",
        words="futures",
    ),  # fmt: skip
    _s(
        "futures.proactive",
        "Speak up on her own",
        "Heads-ups about to-dos that tend to slip. What you asked her to watch for is always said.",
    ),  # fmt: skip
    _s("futures.max_per_hour", "Remarks per hour", "At most this many of her own an hour.", hi=20),
    _s("futures.max_per_day", "Remarks per day", "At most this many of her own a day.", hi=50),
    _s(
        "futures.quiet_hours",
        "Quiet hours for her remarks",
        "No heads-ups, offers or thoughts of her own then, e.g. 22:30-08:00. What you asked her to watch for is "
        "still said. Empty: never quiet.",
        kind="span",
    ),  # fmt: skip
    _s("futures.offers", "Offer help", "\u201cThat essay's due at 11. Want the rubric?\u201d"),
    _s("futures.offers_per_day", "Offers per day", "At most this many.", hi=20),
    _s("futures.context_watch", "Notice the app in front", "App names only, never window titles."),  # fmt: skip
    _s("futures.canvas_watch", "Watch Canvas", "New or changed assignments and grades."),  # fmt: skip
    _s(
        "futures.canvas_every_min",
        "Look at Canvas every",
        "New or moved work is noticed this soon.",
        unit="min",
        hi=180,
    ),
    _s(
        "heads_up.enabled",
        "Tell me what just came in",
        "New or moved work in Canvas and TickTick, and new email: she says what matters, in her own words.",
        words="heads up alerts email notifications canvas new assignment urgent",
    ),  # fmt: skip
    _s(
        "deadlines.enabled",
        "Remind me before deadlines",
        "Canvas work and calendar events as they get close, a little louder each time; the last call reaches your phone.",
        words="deadline due reminder canvas assignment quiz attendance calendar event meeting alert last call",
    ),  # fmt: skip
    _s("heads_up.model", "Model that judges what came in", "", kind="choice", choices=("sonnet", "haiku", "opus")),
    _s(
        "unprompted.enabled",
        "Judge what I bring up",
        "What you waved off stays dropped everywhere, no repeats, nothing stale. Reminders are always said.",
        words="wave off shut up stop bringing up nag repeat quiet unprompted",
    ),  # fmt: skip
    _s("unprompted.model", "Model that judges it", "", kind="choice", choices=("sonnet", "haiku", "opus")),
    _s("heads_up.settle_s", "Wait before judging", "What comes in within this is judged together.", unit="s", hi=600),
    _s("futures.calendar_watch", "Watch your calendar", "Online meetings from your calendar feeds."),  # fmt: skip
    _s(
        "futures.experiments",
        "Experiments",
        "Propose small experiments; nothing runs without your yes.",
        names={"ask": "Propose them", "off": "Never"},
    ),  # fmt: skip
    _s("routines.enabled", "Learn your routines", "Notice what you do around the same time each day."),  # fmt: skip
    _s("routines.offers", "Offer routines", "Off: only learn them (and list them)."),
    _s("study.nudges", "Study nudges", "Ask about studying when an exam is close; offer quick reviews before one."),
    _s("study.exam_days", "Exam nudge window", "Ask when an exam is this close.", unit="days", hi=14),
    _s(
        "proactive.level",
        "Work before you ask",
        "At night, while you're away, she prepares a few things from your week, has each one checked, and hands them "
        "to you in the morning to keep, drop or change. Never sends, submits, buys or deletes.",
        names={
            "off": "Off",
            "suggest": "Suggest only (ask first)",
            "drafts": "Prepare drafts and study sets",
            "projects": "Also code on a branch",
        },
        words="overnight proactive autonomy prepare initiative handoff morning curiosity",
    ),  # fmt: skip
    _s("proactive.max_items", "Things prepared a night", "At most this many.", hi=6),
    _s("proactive.night", "When she works", "Only then, once you've been away a while, e.g. 01:00-08:00.", kind="span"),
    _s(
        "proactive.projects",
        "Projects she may code in",
        "Folder names, for the “also code on a branch” level. Nothing is merged without you.",
        words="project repo branch code",
    ),  # fmt: skip
    _s("proactive.away_min", "Away first", "No turn of yours this long.", unit="min", lo=5, hi=240),
    _s("proactive.look_ahead_days", "Look ahead", "Deadlines and exams this close.", unit="days", hi=14),
    _s("proactive.rank", "Rank with a model", "One cheap call orders what the rules found. Off: rules only."),
    _s("proactive.rank_model", "Ranking model", "The model that orders them."),
    _s("proactive.rank_effort", "Ranking effort", "", names=_EFFORT),
    _s("proactive.steer_min_decisions", "Guesses steer after", "This many of your answers.", hi=200),
    _s("proactive.steer_agreement", "...guessed right this often", "", kind="percent", lo=0.5),
    # ---- Inner life
    _s("inner_life.enabled", "Nightly reflection", "Go over the day each night and keep a journal."),  # fmt: skip
    _s("inner_life.reflect_at", "Reflect at", "When she reflects, e.g. 03:30.", kind="time"),
    _s("inner_life.bring_up", "Bring up a thought", "Mention one early in the next conversation."),
    _s("inner_life.mood", "Mood of her own", "Colors her tone; said only if you ask."),
    _s("inner_life.self_notes", "Notes of her own", "How she's changed, written without asking."),
    _s("inner_life.looking_back", "Monthly look back", "Who she was a month ago, and who she is now."),
    _s("inner_life.dreams", "Dreams", "The odd dream, told when you ask."),
    _s(
        "inner_life.web",
        "Search the web while reflecting",
        "Off keeps her private material away from web pages.",
    ),  # fmt: skip
    # ---- Privacy & recall
    _s(
        "screen_history.enabled",
        "Screen history",
        "The text on your screen, searchable by voice. Text only, kept on this PC.",
        words="total recall",
    ),  # fmt: skip
    _s("screen_history.retention_days", "Keep screen history for", "Older text is deleted.", unit="days"),  # fmt: skip
    _s(
        "finance.enabled",
        "Money",
        "Your transactions, balances, recurring payments, income, loans and credit score, from your statements and "
        "the bank pages you signed in to. Off: nothing is read or kept (delete what's there below).",
        words="finance bank banking usaa cash app mohela student loan credit score budget spending subscriptions",
    ),  # fmt: skip
    _s(
        "finance.heads_ups",
        "Tell me about money that matters",
        "A charge twice, a fee, a late paycheck, money short before payday, a price going up. Never on your phone.",
        words="finance alert overdraft low balance subscription price",
    ),  # fmt: skip
    _s(
        "finance.reads_per_day",
        "Read my bank pages",
        "Times a day she reads the bank pages you signed in to. 0: only when you ask.",
        unit="times a day",
        hi=12,
        words="usaa cash app mohela refresh sync",
    ),  # fmt: skip
    _s(
        "finance.low_balance",
        "Warn below",
        "Money left before your next paycheck below this is worth a word.",
        unit="$",
        words="low balance overdraft runway payday",
    ),  # fmt: skip
    _s(
        "finance.inbox",
        "Statements folder",
        "Statements (CSV, OFX, QFX) you drop here are imported.",
        words="import csv ofx qfx export download",
    ),  # fmt: skip
    _s(
        "screen_history.exclude_apps",
        "Never keep these apps",
        "Besides password managers, e.g. signal, whatsapp. Separate with commas.",
    ),  # fmt: skip
    _s(
        "screen_history.exclude_sites",
        "Never keep these sites",
        "Besides banks, e.g. mychart.org. Separate with commas.",
    ),  # fmt: skip
    _s(
        "screen_history.ocr",
        "Read text from pictures",
        "Windows' offline OCR for apps whose text can't be read directly.",
    ),  # fmt: skip
    _s(
        "futures.week_free_hours_weekday",
        "Free hours on a weekday",
        "What she assumes you can give to work when she looks at whether the week fits.",
        hi=16,
        words="week plan deadlines at risk capacity free time",
    ),  # fmt: skip
    _s(
        "futures.week_free_hours_weekend",
        "Free hours on a weekend day",
        "The same for Saturdays and Sundays.",
        hi=16,
        words="week plan deadlines at risk capacity free time",
    ),  # fmt: skip
    _s(
        "futures.keep_request_words",
        "Keep the words of your requests",
        "Anything that looks secret is cut out.",
    ),  # fmt: skip
    _s(
        "futures.off_limits",
        "Off-limits topics",
        "Domains she never learns about or brings up, e.g. health, finance. Separate with commas.",
    ),  # fmt: skip
    _s("futures.retention_days", "Keep raw events for", "What she learned from them stays.", unit="days"),
    _s("emotion.enabled", "Hear your tone", "A local model reads how you sound. No audio is kept."),  # fmt: skip
    _s("emotion.note", "Tell the brain your tone", "Off: only log it."),
    # ---- Tasks & schedule
    _s(
        "ticktick.changes", "Changing tasks", "Completing, rescheduling, editing or moving tasks.", names=_ASK
    ),  # fmt: skip
    _s("ticktick.deletes", "Deleting tasks", "Deleting tasks, lists or habits.", names=_ASK),
    _s("ticktick.reminders", "Say reminders out loud", "When a task with a time comes due."),
    _s("ticktick.remind_before_min", "Remind me early by", "Minutes before its time.", unit="min"),
    _s(
        "ticktick.morning_summary",
        "Morning rundown",
        "What's due today, around this time. Empty: never.",
        kind="time",
    ),  # fmt: skip
    _s(
        "scheduler.enabled", "Scheduled jobs", "Jobs she runs on her own (\u201cevery weekday at 8...\u201d)."
    ),  # fmt: skip
    _s(
        "scheduler.quiet_hours",
        "Quiet hours for jobs and build sounds",
        "Scheduled jobs due then wait until it's over, and her builds make no sound, e.g. 22:00-07:00. Empty: never.",
        kind="span",
    ),
    _s("scheduler.changes", "Creating or changing jobs", "Deleting one always asks.", names=_ASK),  # fmt: skip
    # ---- Self-improvement
    _s(
        "self_improvement.enabled",
        "Self-improvement",
        "Let her fix and extend her own code.",
        words="evolve builds",
    ),  # fmt: skip
    _s(
        "self_scoring.enabled",
        "Judge her own replies",
        "After each reply: thanked, fine, or corrected, cut short, asked again. No model, no usage.",
        words="self scoring how am i doing",
    ),  # fmt: skip
    _s(
        "self_scoring.experiments",
        "Experiments on herself",
        "Small fair tests of how she talks. Anything that changes your day still asks.",
        names={"own": "Run them herself", "ask": "Ask me first", "off": "Never"},
    ),  # fmt: skip
    _s(
        "self_scoring.strategies",
        "Try new approaches",
        "When the same kind of thing keeps going wrong, she writes an approach, tries it on some turns, and keeps it only "
        "if those go better. Never for school, health, money, work or people.",
        words="strategies evolution scoreboard bandit approaches experiments",
    ),  # fmt: skip
    _s(
        "self_scoring.lessons",
        "Learn from past cases",
        "When something like an earlier request comes up and that went wrong, she sees what she did and how it ended.",
        words="lessons outcomes mistakes experience learn past cases",
    ),  # fmt: skip
    _s(
        "self_scoring.keep_habits",
        "Keep what works",
        "A test that clearly shows a change works better becomes a habit. \u201cDrop that habit\u201d undoes it.",
    ),  # fmt: skip
    _s(
        "self_improvement.auto_build",
        "Build on her own",
        "Off: only when you ask. Errors: also fix her own crashes. All: also build features she notes.",
        names={"off": "Only when I ask", "errors": "Fix errors", "all": "Everything"},
    ),  # fmt: skip
    _s(
        "self_improvement.auto_install",
        "Install on her own",
        "Safe changes: install when checks pass and nothing sensitive changed.",
        names={"never": "Always ask", "safe_changes": "Safe changes"},
    ),  # fmt: skip
    _s("self_improvement.max_builds_per_day", "Builds per day", "Each build uses Claude usage.", hi=200),
    _s("self_improvement.parallel_builds", "Builds at once", "Each uses Claude usage meanwhile."),
    _s(
        "self_improvement.builder_model",
        "Builder model",
        "The coding agent that writes changes.",
        kind="choice",
        choices=("opus", "sonnet", "haiku"),
    ),  # fmt: skip
    _s("self_improvement.builder_effort", "Builder effort", "", names=_EFFORT),
    _s(
        "self_improvement.nightly_at",
        "Night shift starts",
        "Reads the day and starts fixes. Empty: never.",
        kind="time",
    ),  # fmt: skip
    _s(
        "self_improvement.morning_report",
        "Morning report",
        "The night ends here; you hear what she did.",
        kind="time",
    ),  # fmt: skip
    _s(
        "self_improvement.announce",
        "When a build is ready",
        "",
        names={"speak": "Tell me out loud", "console": "Only in the console"},
    ),  # fmt: skip
    _s("usage_budget.enabled", "Usage budget", "Keep her background work to a share of your Claude limit."),
    _s(
        "usage_budget.daily_share",
        "Daily share of your weekly limit",
        "Her background work per day. 0: none at all.",
        kind="percent",
        hi=0.5,
    ),  # fmt: skip
    _s("usage_budget.max_five_hour", "Pause when 5-hour window is", "This full or more.", kind="percent"),
    _s("usage_budget.max_weekly", "Pause when weekly limit is", "This full or more.", kind="percent"),
    _s(
        "objectives.enabled",
        "Objectives",
        "Hand her an outcome; she plans it, runs the steps, has each one checked and tells you once it's over.",
        words="goals autonomous agents delegate plan",
    ),  # fmt: skip
    _s(
        "objectives.respect_limits",
        "Objectives wait for your limits",
        "Hold their steps back while your 5-hour or weekly limit is as full as above.",
    ),  # fmt: skip
    _s("objectives.max_open", "Objectives at once", "How many can be under way.", hi=10),
    _s(
        "deep_think.enabled",
        "Think hard questions through",
        "For a hard question she takes a minute to think it over at a higher effort, then tells you.",
        words="deep think reasoning effort smart",
    ),  # fmt: skip
    _s("deep_think.effort", "Effort for hard questions", "How hard she thinks them over.", names=_EFFORT),
    _s(
        "deep_think.max_second_opinions",
        "Second opinions a day",
        "How often she may have another mind argue against something on her own. The ones you ask for don't count.",
        words="critic pushback devil's advocate second opinion disagree",
    ),  # fmt: skip
    _s(
        "deep_think.max_per_day",
        "Hard questions a day on her own",
        "How many she may take away to think about without being asked. 0: only when you ask.",
        hi=50,
    ),  # fmt: skip
    _s(
        "playbooks.enabled",
        "Learn each app and site",
        "After a screen or web task she keeps short notes on the app or site, and the next task there starts with them.",
        words="playbook playbooks notes learn apps sites screen tasks web tasks",
    ),  # fmt: skip
    _s(
        "skills.enabled",
        "Skills",
        "Her tasks can save steps that worked (on your yes) and run them again in one go.",
        words="skill skills steps macro shortcut repeat screen tasks web tasks",
    ),  # fmt: skip
    _s(
        "skills.notice_repeats",
        "Offer skills for repeated steps",
        "When she keeps doing the same steps by hand in her tasks, she offers to save them as a skill.",
        words="skill skills repeat repeated steps offer notice",
    ),  # fmt: skip
    # ---- Maps
    _s(
        "gods_eye.enabled",
        "Show places in God's Eye View",
        "“Show me ...” opens the place on its 3D globe in your browser. Off: a satellite picture window.",
        words="gods eye view satellite globe map location",
    ),
    _s(
        "gods_eye.folder",
        "God's Eye View folder",
        "Where it's installed. Empty: gods-eye-view next to her own folder.",
    ),
    _s(
        "gods_eye.address",
        "God's Eye View address",
        "Where its local server answers. She starts it when it doesn't.",
    ),
    # ---- Pop-up panels
    _s(
        "visuals.enabled",
        "Pop up visuals",
        "She can put a diagram, chart or formatted note on the screen while she explains something.",
        words="visual visuals panel panels diagram chart popup pop-up explain show",
    ),
    _s(
        "visuals.stay_s",
        "How long a panel stays",
        "Paused while the pointer is on it. 0: until you close it.",
        unit="s",
        hi=3600,
    ),
    _s("visuals.max_panels", "Panels at once", "A new one closes the oldest past this."),
    _s(
        "visuals.hide_from_capture",
        "Hide panels from screenshots",
        "Keep them out of screenshots (hers too), recordings and screen shares.",
    ),
    _s(
        "visuals.idle_quit_s",
        "Close the panel app when idle",
        "With no panel up this long, it closes to save memory. 0: never.",
        unit="s",
        hi=86400,
    ),
    _s(
        "visuals.status_panel",
        "Status panel",
        "What's running (downloads, tasks, updates, objectives). Auto: it comes up by itself; ask: only for "
        "“what's running?”; off: never.",
        words="status running progress downloads jobs tasks builds objectives background",
    ),
    _s(
        "visuals.marks",
        "Draw on the screen",
        "She can put arrows, rings, highlights and labels over your screen to show you where to click. "
        "Your stop shortcut clears them.",
        words="marks mark draw arrow circle highlight label guide overlay point show where click",
    ),
    _s(
        "visuals.marks_stay_s",
        "How long marks stay",
        "Then they clear by themselves. 0: until she or you clears them.",
        unit="s",
        hi=3600,
    ),
    _s(
        "circling.enabled",
        "Circle things to mean “this”",
        "Move the mouse in a loop round something, no buttons held, then talk: it's what she takes you to mean. "
        "The picture only goes anywhere if you talk to her before the circle lapses.",
        words="circle mouse gesture this that point pointer cursor loop ring",
    ),
    _s(
        "circling.linger_s",
        "How long a circle counts",
        "After this long she no longer takes it to be what you mean.",
        unit="s",
        lo=3,
        hi=300,
    ),
    _s(
        "circling.ring",
        "Show a ring when she sees it",
        "A faint blue ring round what you circled, gone in about a second.",
        words="circle mouse gesture ring acknowledge",
    ),
    _s(
        "visuals.web_images",
        "Pictures from the web",
        "“Show me ...” a character, animal or thing: she finds a picture and puts it on a panel, with where it's "
        "from, instead of opening image results in your browser.",
        words="image images picture pictures photo photos web search show google images",
    ),
    _s(
        "visuals.image_stay_s",
        "How long a picture stays",
        "Paused while the pointer is on it. 0: until you close it.",
        unit="s",
        hi=3600,
    ),
    _s(
        "visuals.safe_search",
        "Picture search filter",
        "How strictly the picture search leaves out adult images.",
        words="safe search safesearch adult filter explicit nsfw",
    ),
    _s(
        "visuals.shelf",
        "Keep closed panels",
        "So you can bring one back from her chat window, her tray or by asking. Pinned ones are kept either way.",
    ),
    _s(
        "visuals.shelf_days",
        "Days to keep closed panels",
        "After this they're gone from this PC.",
    ),
    _s(
        "visuals.explainer_orb_points",
        "She points along as she explains",
        "When a panel plays step by step as she talks (an explainer), she flies over and points at what each step "
        "is about, as well as the glowing ring on the panel.",
        words="explainer explain point pointing orb walkthrough animation steps",
    ),
    _s(
        "visuals.web_pages",
        "Web pages on a panel",
        "She can show one web page live on a panel (in a sandbox with none of your logins) when a quick look "
        "suits better than your browser. Asking to browse or open a site still opens your browser.",
        words="web page website browser embed webview panel show site",
    ),
    _s(
        "visuals.page_stay_s",
        "How long a web page stays",
        "Paused while the pointer is on it. 0: until you close it.",
        unit="s",
        hi=3600,
    ),
    # ---- Phone & Discord
    _s("discord_bridge.enabled", "Discord bridge", "Text her from your phone through your Discord bot."),  # fmt: skip
    _s(
        "discord_bridge.owner_id",
        "Your Discord user ID",
        "Only this account is ever answered. 0: the one in .env.",
    ),  # fmt: skip
    _s(
        "discord_bridge.notify",
        "Text me her announcements",
        "Builds, timers, reminders, tasks done.",
        names={"off": "Never", "away": "When I'm away", "always": "Always"},
    ),  # fmt: skip
    _s("discord_bridge.away_after_min", "Away after", "No keyboard or mouse this long.", unit="min"),
    _s("discord_bridge.speak_replies", "Say texted replies out loud", "Also at the PC."),
    _s(
        "discord_bridge.voice_calls",
        "Join voice calls",
        "Join your voice channel when you join one in a server her bot is in.",
    ),  # fmt: skip
    _s("discord_bridge.mute_pc_on_call", "Quiet PC on calls", "Her voice goes to the call only."),
    _s(
        "discord_bridge.call_wake_word",
        "Need her name on Discord calls",
        "On a call with her in your server. Off: everything you say is for her.",
    ),  # fmt: skip
    # ---- System
    _s(
        "system.tray_icon",
        "Icon by the clock",
        "Turned off, her chat shortcut still opens her chat.",
        words="tray",
    ),  # fmt: skip
    _s(
        "system.explorer_menu",
        "In Explorer's right-click menu",
        "Ask her about a file, have it summarized, or talk to her, from a right-click. Windows 11: Show more options.",
        words="context menu explorer right click shell",
    ),  # fmt: skip
    _s(
        "system.revive",
        "Come back if something kills her",
        "A scheduled task checks every two minutes and starts her again. Shutting her down yourself keeps her off.",
        words="watchdog crash revive scheduled task supervisor dead",
    ),  # fmt: skip
    _s(
        "system.restart_for_new_code",
        "Restart into new code",
        "When code changes outside her, restart into it once she's idle.",
    ),  # fmt: skip
    _s(
        "system.new_code_quiet_s",
        "Wait for code to settle",
        "How long nothing changes before she restarts.",
        unit="s",
    ),  # fmt: skip
    _s(
        "logging.level",
        "Log detail",
        "How much goes in her log file.",
        kind="choice",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        names={"DEBUG": "Everything", "INFO": "Normal", "WARNING": "Warnings", "ERROR": "Errors only"},
    ),  # fmt: skip
    _s("logging.show_replies", "Show replies in the console", ""),
    # ---- Shown since the settings window's second overhaul (they were only in the config files)
    _s(
        "tts.fish_audio.transport",
        "Connection to Fish",
        "HTTP starts her sentences sooner (0.6 s to the first sound against 1.0 s over the WebSocket, measured "
        "2026-11-22); the WebSocket falls back to HTTP when it can't connect.",
        names={"websocket": "WebSocket", "http": "HTTP"},
    ),
    _s(
        "tts.fallback_cooldown_s",
        "Stay on the backup for",
        "After a switch, keep to the backup voice this long before trying Fish again.",
        unit="s",
    ),
    _s(
        "avatar.point_linger_s",
        "Stay after pointing",
        "How long she stays by what she pointed at before flying home.",
        unit="s",
    ),
    _s(
        "vad.threshold",
        "Speech detection",
        "How sure she must be that a sound is speech. Higher: fewer false starts from noise.",
        step=0.05,
        words="mic sensitivity noise",
    ),
    _s("vad.min_speech_ms", "Shortest speech", "Sounds shorter than this aren't taken for talking.", unit="ms"),
    _s("vad.pre_roll_ms", "Audio kept from before", "So the first word isn't clipped.", unit="ms"),
    _s(
        "barge_in.vad_threshold",
        "Interrupt detection",
        "How sure she must be you're talking over her. Higher: her own echo stops her less.",
        step=0.05,
    ),
    _s("barge_in.min_speech_ms", "Talk this long to interrupt", "", unit="ms"),
    _s(
        "barge_in.aec_warmup_s",
        "Echo cancellation warm-up",
        "On speakers, talking over her waits this long into a reply while the echo canceller learns the room.",
        unit="s",
    ),
    _s("stt.num_threads", "Speech recognition threads", "CPU threads for turning your words into text.", hi=32),
    _s(
        "fast_chat.context_turns",
        "Quick answers: lines of context",
        "How much of the conversation the quick voice sees.",
        hi=40,
    ),
    _s("deep_think.model", "Model for hard questions", "", kind="choice", choices=("opus", "sonnet", "haiku")),
    _s("deep_think.time_limit_min", "Time for a hard question", "She gives an answer by then.", unit="min"),
    _s("deep_think.web", "Search the web for hard questions", ""),
    _s("playbooks.model", "Model that writes app notes", "", kind="choice", choices=("sonnet", "haiku", "opus")),
    _s("playbooks.effort", "Effort for app notes", "", names=_EFFORT),
    _s("playbooks.min_steps", "Notes after tasks of at least", "Shorter tasks teach too little.", unit="steps"),
    _s(
        "claims.mode",
        "Check what she says she did",
        "Compare her “done” with what her tools really did. Hold her to it: a “sent” nothing confirmed is said "
        "honestly instead. Tell her: she hears about it next turn. Log: only write it down.",
        names={"off": "Off", "log": "Log", "note": "Log and tell her", "enforce": "Hold her to it"},
        words="honesty lies verify",
    ),
    _s(
        "brain.claude.warm_cache",
        "Keep her prompt warm",
        "A tiny call now and then keeps Claude's copy of her prompt cached, so the next answer starts sooner.",
    ),
    _s(
        "brain.claude.resume_last_session",
        "Pick up after a restart",
        "Continue the previous conversation when she starts again.",
    ),
    _s("brain.claude.connect_timeout_s", "Give up connecting after", "", unit="s"),
    _s(
        "brain.claude.stall_timeout_s",
        "A silent brain is stuck after",
        "No word from Claude this long: she stops waiting and says so.",
        unit="s",
    ),
    _s(
        "local_brain.enabled",
        "Local brain",
        "Run the model on this PC's GPU beside Claude.",
        words="qwen llama",
    ),
    _s(
        "local_brain.mode",
        "What it does",
        "Shadow: only notes what it would have done. Route: answers simple commands itself and hands the rest to "
        "Claude. Fallback only: answers just while Claude is down.",
        names={"shadow": "Shadow", "route": "Route", "fallback_only": "Fallback only"},
        words="routing router",
    ),
    _s("local_brain.model", "Model file", "A GGUF file. Missing: the local brain stays off.", words="gguf qwen"),
    _s("local_brain.server", "llama.cpp server", "llama-server.exe (the Vulkan build).", words="llama"),
    _s("local_brain.device", "GPU", "As `llama-server --list-devices` names it, e.g. Vulkan0."),
    _s(
        "local_brain.context",
        "Context size",
        "Tokens it can read at once. Her prompt and tools take about 42k.",
        unit="tokens",
        step=4096,
    ),
    _s(
        "local_brain.route_max_words",
        "Longest turn it answers",
        "Longer requests always go to Claude.",
        unit="words",
    ),
    _s(
        "local_brain.uncensored",
        "Uncensored when local only",
        "Set to local brain only, she goes along with adult or explicit talk instead of turning it down. Claude is "
        "never told.",
        words="nsfw adult explicit",
    ),
    _s(
        "local_brain.route_min_confidence",
        "How sure it must be",
        "It answers only when at least this sure a turn is its own; otherwise Claude does.",
        kind="percent",
    ),
    _s(
        "local_brain.answer_timeout_s",
        "Hand over to Claude after",
        "A routed answer slower than this goes to Claude instead.",
        unit="s",
        step=0.5,
    ),
    _s("local_brain.history_turns", "Earlier exchanges it sees", "So a follow-up makes sense."),
    _s(
        "local_brain.fallback_timeout_s",
        "Time it gets while Claude is down",
        "Then nothing else can answer, so it may take longer.",
        unit="s",
    ),
    _s(
        "local_brain.claude_retry_s",
        "Try Claude again after",
        "After Claude failed, your turns go to the local model first for this long.",
        unit="s",
    ),
    _s(
        "local_brain.game_unload_s",
        "Unload for a game after",
        "A game in front this long gives the GPU's memory back to it. 0: never.",
        unit="s",
    ),
    _s("local_brain.game_reload_s", "Load again after", "No game in front for this long.", unit="s"),
    _s(
        "local_brain.route_tools",
        "Tools it may use",
        "What a routed answer may do by itself. Separate with commas.",
    ),
    _s("local_brain.port", "Port", "On 127.0.0.1 only."),
    _s(
        "local_brain.slot_cache_dir",
        "Prompt cache folder",
        "Keeps its read prompt (~1.5 GB), so it's back in a second after a game. Empty: read it again each time.",
    ),
    _s("addressee.enabled", "Notice talk meant for her", "Judge what you say near the PC without her name."),
    _s(
        "addressee.threshold",
        "Sure enough at",
        "How sure the model must be that it was for her to count it.",
        kind="percent",
    ),
    _s(
        "addressee.repeat_window_s",
        "Said again within",
        "Repeated with her name this soon: it was for her.",
        unit="s",
    ),
    _s(
        "self_improvement.nightly_builds",
        "Night-shift builds",
        "Fixes she may start overnight from the day's patterns.",
        hi=10,
    ),
    _s(
        "self_improvement.frontier_weekly",
        "Weekly check on past mistakes",
        "Once a week overnight she asks her real past mistakes again: how many she gets right now, and fixes for "
        "the rest. About 2 % of a week's usage.",
        words="growth progress smarter frontier",
    ),
    _s(
        "memory.enabled",
        "Remember what you tell her",
        "Facts about you, kept on this PC, that she brings into conversations.",
        words="memories facts",
    ),
    _s(
        "memory.max_prompt_items",
        "Memories she keeps in mind",
        "How many go into each conversation. More: she knows more at once, and answers a little slower.",
        hi=1000,
    ),
    _s(
        "screen_history.max_cpu_percent",
        "Skip a look when the PC is busier than",
        "",
        unit="%",
    ),
    _s("screen_history.max_mb", "Screen history size limit", "Oldest text goes first.", unit="MB"),
    _s(
        "screen_history.idle_after_s",
        "Stop looking when idle for",
        "No keyboard or mouse this long.",
        unit="s",
    ),
    _s(
        "emotion.min_confidence",
        "Tone sure enough at",
        "Only a tone this sure is told to the brain.",
        kind="percent",
    ),
    _s("emotion.prosody", "Loudness, pitch and pace", "Compare them with how you usually sound."),
    _s("futures.cooldown_h", "Same heads-up again after", "", unit="h"),
    _s("futures.heads_up_h", "Heads-up this early", "Before something that tends to slip.", unit="h"),
    _s("futures.offer_assignment_h", "Offer help with an assignment", "This long before it's due.", unit="h"),
    _s("futures.offer_meeting_min", "Offer help before a meeting", "This long before it starts.", unit="min"),
    _s(
        "deadlines.canvas_h",
        "Deadline reminders at",
        "Hours before Canvas work is due, furthest first; the last is the last call. Separate with commas.",
    ),
    _s("deadlines.event_min", "Say an event is starting", "This long before it starts (0: never).", unit="min"),
    _s(
        "futures.work_apps",
        "Work apps",
        "Apps where you're writing or working: she holds remarks while one is in front. Separate with commas.",
    ),
    _s("routines.mine_at", "Look for routines at", "Each day, e.g. 04:15.", kind="time"),
    _s("routines.history_days", "Routines: days looked back", "", unit="days"),
    _s("routines.min_days", "Routines: seen on at least", "", unit="days"),
    _s("routines.offers_per_day", "Routine offers per day", ""),
    _s("routines.exclude_apps", "Routines: ignore these apps", "Separate with commas."),
    _s("study.enabled", "Study tools", "Trackers, quizzes and study sessions per course.", words="school exam"),
    _s(
        "study.canvas_exams",
        "Find exams in Canvas",
        "Look for midterms and finals in Canvas's planner every few hours.",
    ),
    _s("study.min_reviews", "Quick review when this many are due", "Missed quiz questions due again.", hi=20),
    _s("ticktick.check_every_min", "Check TickTick every", "No Claude usage.", unit="min"),
    _s("inner_life.max_per_day", "Reflections per day", ""),
    _s("inner_life.idle_minutes", "Reflect once you're away for", "", unit="min"),
    _s("inner_life.time_limit_min", "Time for a reflection", "", unit="min"),
    _s(
        "objectives.project_roots",
        "Your project folders",
        "Where objectives may write code, on branches of their own. Separate with commas.",
        words="code repos",
    ),
    _s("objectives.web", "Objectives may search the web", ""),
    _s("objectives.model", "Model for objectives", "", kind="choice", choices=("opus", "sonnet", "haiku")),
    _s("objectives.effort", "Effort for objectives", "", names=_EFFORT),
    _s(
        "objectives.review_model",
        "Model that checks each step",
        "",
        kind="choice",
        choices=("opus", "sonnet", "haiku"),
    ),
    _s("objectives.review_effort", "Effort for checking", "", names=_EFFORT),
    _s("objectives.research_parallel", "Research steps at once", ""),
    _s("objectives.code_timeout_min", "Time for a code step", "", unit="min"),
    _s("objectives.check_timeout_min", "Time for a check", "", unit="min"),
    _s("self_improvement.build_timeout_min", "Time for a build", "", unit="min"),
    _s("self_improvement.retry_failed_checks", "Retry failed checks once", "One more round when the final tests fail."),
    _s("self_improvement.notice_errors", "Notice her own errors", "Errors in her code become things to fix."),
    _s("discord_bridge.confirm_timeout_s", "Wait for a texted yes", "", unit="s"),
    _s("discord_bridge.max_turns_per_10_min", "Texts answered per 10 minutes", "A runaway guard."),
    _s(
        "logging.console",
        "Console detail",
        "",
        names={"quiet": "Quiet", "normal": "Normal", "verbose": "Everything"},
    ),
    _s("skills.min_tasks", "Offer a skill after", "Tasks the same steps must turn up in first.", unit="tasks"),
    _s("skills.min_steps", "Shortest skill", "Fewer steps aren't worth saving.", unit="steps"),
    _s("fast_chat.timeout_s", "Quick answer gives up after", "Then the main brain answers.", unit="s", step=0.5),
    _s(
        "tools.search_engine",
        "Search engine",
        "Where she searches the web for you.",
        kind="choice",
        choices=("google", "bing", "duckduckgo", "youtube"),
    ),
    _s(
        "system.disable_power_throttling",
        "Full speed in the background",
        "Windows' power saving can slow her down when she isn't in front.",
    ),
)


# What each key is called in the window.
_ENV_LABELS = {
    "FISH_AUDIO_API_KEY": "Fish Audio API key",
    "FISH_AUDIO_VOICE_ID": "Fish Audio voice ID",
    "ANTHROPIC_API_KEY": "Anthropic API key",
    "OPENAI_API_KEY": "Another model's API key",
    "TICKTICK_API_TOKEN": "TickTick token",
    "CANVAS_API_TOKEN": "Canvas token",
    "CANVAS_BASE_URL": "Canvas address",
    "GMAIL_ADDRESS": "Gmail address",
    "GMAIL_APP_PASSWORD": "Gmail app password",
    "CALENDAR_ICS_URLS": "Calendar addresses",
    "SPOTIFY_CLIENT_ID": "Spotify client ID",
    "AUDD_API_TOKEN": "AudD token (song recognition)",
    "DISCORD_BOT_TOKEN": "Discord bot token",
    "DISCORD_OWNER_ID": "Your Discord user ID",
    "CESIUM_ION_TOKEN": "Cesium ion token (God's Eye 3D)",
    "GOOGLE_MAPS_API_KEY": "Google Maps key (God's Eye 3D)",
}


def _env_settings() -> tuple[Setting, ...]:
    from octavia import keys

    out = []
    for key in keys.KEYS:
        if key.name in ("OCTAVIA_CONFIG", "OCTAVIA_MODELS_DIR"):
            continue  # where her files live: not something to change from inside them
        secret = key.noun not in ("address", "voice id", "user ID")
        out.append(
            Setting(
                ENV + key.name,
                _ENV_LABELS.get(key.name, f"{key.service} {key.noun}"),
                key.about,
                kind="secret" if secret else "envtext",
                restart=key.restart,
                words=key.name,
            )
        )
    return tuple(out)


_ALL: tuple[Setting, ...] | None = None


def unavailable() -> frozenset[str]:
    """Settings whose code this build left out (the public build, src/octavia/release/): never shown."""
    import importlib.util

    gone = set()
    if importlib.util.find_spec("octavia.localbrain.uncensored") is None:
        gone.add("local_brain.uncensored")
    return frozenset(gone)


def all_settings() -> tuple[Setting, ...]:
    """Every setting, in the window's order, each with its section and group from ``SECTIONS``. One the map leaves
    out isn't shown (``test_every_setting_has_one_place`` keeps that from happening by accident)."""
    global _ALL
    if _ALL is None:
        known = {s.key: s for s in SETTINGS + _env_settings()}
        for key in unavailable():
            known.pop(key, None)
        placed = []
        for section in SECTIONS:
            for group in section.groups:
                for key in group.keys:
                    if key in known:
                        placed.append(
                            replace(
                                known[key],
                                section=section.id,
                                group=group.title,
                                advanced=group.advanced,
                                when=group.when,
                            )  # fmt: skip
                        )
        _ALL = tuple(placed)
    return _ALL


def section_of(section_id: str) -> Section:
    return next(s for s in SECTIONS if s.id == section_id)


def by_key(key: str) -> Setting | None:
    return next((s for s in all_settings() if s.key == key), None)


def config_keys() -> frozenset[str]:
    return frozenset(s.key for s in SETTINGS)


def needs_restart(setting: Setting) -> bool:
    """Whether she only picks it up when she starts (``core/live_settings.py`` applies the rest while she runs)."""
    if setting.env:
        return setting.restart
    from octavia.core.live_settings import HOOKED, LIVE

    return setting.key not in LIVE and setting.key not in HOOKED


# ---- what the config's types say


def field_info(key: str) -> Any:
    """The pydantic FieldInfo for a dotted config key."""
    from octavia.config import AppConfig

    model: Any = AppConfig
    info = None
    for part in key.split("."):
        info = model.model_fields[part]
        model = _model_of(info.annotation)
    return info


def _model_of(annotation: Any) -> Any:
    from pydantic import BaseModel

    for candidate in (annotation, *typing.get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _bare(annotation: Any) -> tuple[Any, bool]:
    """The type without ``| None``, and whether None is allowed."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        optional = len(args) < len(typing.get_args(annotation))
        if len(args) == 1:
            return args[0], optional
        return annotation, optional
    return annotation, False


def optional(setting: Setting) -> bool:
    if setting.env:
        return True
    return _bare(field_info(setting.key).annotation)[1]


def kind_of(setting: Setting) -> str:
    if setting.kind:
        return setting.kind
    bare, _ = _bare(field_info(setting.key).annotation)
    if bare is bool:
        return "bool"
    if typing.get_origin(bare) is Literal or (isinstance(bare, type) and issubclass(bare, enum.Enum)):
        return "choice"
    if bare is int:
        return "int"
    if bare is float:
        return "float"
    if typing.get_origin(bare) is list:
        return "list"
    return "text"


def choices_of(setting: Setting) -> tuple[str, ...]:
    if setting.choices:
        return setting.choices
    bare, _ = _bare(field_info(setting.key).annotation)
    if typing.get_origin(bare) is Literal:
        return tuple(str(a) for a in typing.get_args(bare))
    if isinstance(bare, type) and issubclass(bare, enum.Enum):
        return tuple(str(m.value) for m in bare)
    return ()


def bounds(setting: Setting) -> tuple[float | None, float | None]:
    """The lowest and highest value the config accepts (the setting's own, where it narrows them)."""
    lo = hi = None
    if not setting.env:
        for meta in field_info(setting.key).metadata:
            for name in ("ge", "gt"):
                if getattr(meta, name, None) is not None:
                    lo = getattr(meta, name)
            for name in ("le", "lt"):
                if getattr(meta, name, None) is not None:
                    hi = getattr(meta, name)
    return (setting.lo if setting.lo is not None else lo), (setting.hi if setting.hi is not None else hi)


def shown_choice(setting: Setting, value: str) -> str:
    return setting.names.get(value, value)


# What people call a setting that its own words don't say.
SYNONYMS = {
    "dnd": "quiet snooze",
    "disturb": "quiet snooze",
    "silent": "quiet",
    "silence": "quiet",
    "sleep": "quiet",
    "night": "quiet",
    "loud": "volume",
    "louder": "volume",
    "quieter": "volume",
    "tts": "voice fish",
    "speech": "voice",
    "captions": "subtitles",
    "caption": "subtitles",
    "llm": "brain model",
    "ai": "brain model",
    "gpu": "local",
    "qwen": "local",
    "offline": "local",
    "hotkey": "shortcut",
    "hotkeys": "shortcuts",
    "keybind": "shortcut",
    "mic": "microphone",
    "avatar": "look orb character",
    "appearance": "look",
    "theme": "look",
    "limit": "usage budget",
    "limits": "usage budget",
    "quota": "usage budget",
    "cost": "usage budget",
    "token": "key",
    "password": "key",
    "api": "key",
}


def _words(text: str) -> list[str]:
    return [w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split() if w]


def score(setting: Setting, query: str) -> int:
    """How well a search finds this setting, 0 for not at all: every word of ``query`` (or a word it's a synonym for)
    must start a word somewhere in its label, description, group, section or key; one in the label counts most."""
    words = _words(query)
    if not words:
        return 1
    section = next((s for s in SECTIONS if s.id == setting.section), None)
    label = _words(setting.label)
    places = [
        (label, 8),
        (_words(setting.group + " " + setting.words), 4),
        (_words(setting.key.replace("_", " ") + " " + setting.about), 2),
        (_words(f"{section.title} {section.words}") if section else [], 1),
    ]
    total = 0
    for word in words:
        best = 0
        for candidate in (word, *SYNONYMS.get(word, "").split()):
            for hay, weight in places:
                if any(h == candidate for h in hay):
                    best = max(best, weight * 2)
                elif any(h.startswith(candidate) for h in hay):
                    best = max(best, weight)
        if not best:
            return 0
        total += best
    phrase = " ".join(words)
    if phrase in setting.label.lower():
        total += 10
    return total


def search(query: str) -> list[Setting]:
    """The settings ``query`` finds, best first (the window's order among equals)."""
    order = {s.key: i for i, s in enumerate(all_settings())}
    found = [(score(s, query), s) for s in all_settings()]
    best = max((n for n, _ in found), default=0)
    # Far weaker matches than the best (a word deep in a description) would bury what was meant.
    kept = [(n, s) for n, s in found if n and n >= best * 0.3]
    return [s for n, s in sorted(kept, key=lambda p: (-p[0], order[p[1].key]))]


def sections_found(query: str) -> list[str]:
    """The sections a search names (by their title or their own words, or a synonym of one), for "Go to" links."""
    words = _words(query)
    if not words:
        return []
    out = []
    for section in SECTIONS:
        hay = _words(f"{section.title} {section.words}")
        if all(any(h.startswith(c) for h in hay for c in (w, *SYNONYMS.get(w, "").split())) for w in words):
            out.append(section.id)
    return out


def matches(setting: Setting, query: str) -> bool:
    """Whether a search finds this setting."""
    return score(setting, query) > 0
