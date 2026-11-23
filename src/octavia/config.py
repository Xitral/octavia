"""Configuration: YAML files + environment, validated with pydantic.

Load order (later wins):

1. ``config/default.yaml``: committed defaults, documented.
2. ``config/local.yaml``: your personal overrides (git-ignored), or the
   file named by the ``OCTAVIA_CONFIG`` environment variable.
3. ``data/settings.json``: what was changed from her windows (the tray, the settings window), each choice kept
   only while the files above still give what they gave when it was made (``octavia.user_settings``).
4. Programmatic overrides (e.g. CLI flags).

Secrets never live in YAML. Providers read them from environment variables,
and a git-ignored ``.env`` file in the project root is loaded automatically.
Unknown keys are rejected so typos surface immediately.

Relative paths are anchored at the project root, except that paths under
``models/`` resolve inside ``OCTAVIA_MODELS_DIR`` when that environment variable
is set (and not empty) and the config's root is the checkout this package was
imported from. Self-improvement sets it for a build's git worktree, which has no
copy of the git-ignored, downloaded models, so the model tests there use the
main checkout's models. A config on any other root (a test's temporary folder)
keeps its own ``models/``, so it can never overwrite the shared ones.
"""

from __future__ import annotations

import codecs
import copy
import io
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from octavia import edition
from octavia.tools.permissions import Decision

log = logging.getLogger(__name__)

MODELS_DIR_ENV = "OCTAVIA_MODELS_DIR"  # where "models/..." paths resolve instead of <root>/models
_MODELS = "models"


def find_project_root() -> Path:
    env = os.environ.get("OCTAVIA_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "default.yaml").exists():
            return parent
    return Path.cwd()


# The checkout this package was imported from (or OCTAVIA_HOME): the only root whose models/ follows
# OCTAVIA_MODELS_DIR.
_CHECKOUT_ROOT = find_project_root().resolve()


def _is_checkout(root: Path) -> bool:
    try:
        return root.resolve() == _CHECKOUT_ROOT
    except OSError:
        return False


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


DeviceRef = int | str | None

# Claude Code built-in tools that only read (web). Everything else must be a project Tool.
READ_ONLY_BUILTINS = frozenset({"WebSearch", "WebFetch"})
# brain.claude.always_loaded_tools by default (see there).
ALWAYS_LOADED_TOOLS = (
    "second_opinion",  # she doesn't reach for a tool she'd first have to look up, as with think_hard
    "list_improvements",
    "note_improvement",
    "build_improvement",
    "install_improvement",
    "remember",
    "recall",
    "look_at_screen",
    "read_page",  # all of a page at once, rather than a screenful at a time (two caught complaints)
    "find_on_screen",
    # Looked up on demand most often (each lookup is another model round trip before she can act):
    "zoom_screen",
    "wait_for_screen_change",
    "start_screen_task",
    "resume_screen_task",
    "point_sequence",
    "mark_screen",  # she marks what she explains on screen unasked: a lookup first would make her skip it or lag
    "guide_on_screen",
    "click",
    "scroll",
    "type_text",
    "press_keys",
    "media_control",
    "play_on_spotify",
    "open_url",
    "show_image",  # "show me <a character>": loaded, or she'd reach for open_url and Google Images instead
    "open_application",
    "read_document",  # what her Explorer menu's Summarize, Explain, Study and Ask read with: a lookup first cost ~1.5 s
    "think_hard",  # looked up on demand, she answered hard questions herself rather than search for it
    # Looked up on demand most often from 2026-10-15 to 10-25 (about one turn in eight looked one up first, at about
    # 1.5 s each); together about 2k tokens more per request, nearly all of it read from the cache. Not drag, the most
    # looked up: its description alone is 700 tokens, for a few screen tasks a week.
    "open_phone_attachment",
    "copy_to_clipboard",
    "garmin_vitals",
    "canvas_due",
    "canvas_grades",
    "start_background_task",
    "show_note",
    "get_weather",
    "search_gmail",
    "study_status",  # where every study session starts
    "quiz_record",  # called for every question of a quiz: a lookup each time would slow the back-and-forth
    "check_practice",  # graded after every practice answer
    "walk_through",  # one call per step of a walkthrough
    # Looked up most from 2026-10-21 to 10-26 (her log: one brain turn in five looked a tool up first; 1 to 2.5 s per
    # lookup, measured). A bigger set barely moved her first word (14k to 91k tokens, same within noise), but every
    # different set re-reads the conversation uncached, so the set is fixed, not chosen per request. About 2.5k
    # tokens; set_volume left (one brain use in 1,242 turns: quick commands answer it). Not drag (900 tokens) or
    # show_visual (5.9k): a lookup is cheaper than carrying them on every request. Not TickTick's: they only exist
    # with its key, and a name here that matches no tool is warned about at every start.
    "focus_window",
    "run_command",
    "practice_problem",
    "practice_worksheet",
    "canvas_course_material",
    "canvas_announcements",
    "reject_improvement",
    "correct_memory",
    "restart_now",
    "find_files",
    # Looked up most from 2026-10-26 to 10-28 (34 of 329 brain turns, 8.7 s median to the first word): her panels.
    # look_at_panel, update_visual and list_visuals (about 1.7k tokens together) are used turn after turn once a panel
    # is up, and switch_engines is small (215); 2.9k tokens more per turn as the eval read it. Not show_visual (5k) or explain (7.7k): above the eval gate's limit.
    "look_at_panel",
    "update_visual",
    "list_visuals",
    "switch_engines",
    # A wave-off ("shut up about that") is said in passing and must land: on 2026-10-28 she reached for remember,
    # which the study nudges never read, and a lookup first could make her skip it. Small (about 400 tokens).
    "wave_off",
    # Looked up most from 2026-11-15 to 11-22 beside show_visual (56 lookups in 7 days, 3.3 s median each; 29 % of
    # tool turns on 11-20..22 looked one up first): small ones, about 950 tokens together. show_visual (6.3k) and
    # explain (9.1k) stay on demand, over the eval gate's 3k limit.
    "study_update",
    "list_screen_tasks",
    "stop_screen_task",
    "explainer_control",
    "bring_back_visual",
    "clear_marks",
)


class AssistantConfig(_Section):
    name: str = "Computer"
    wake_phrase: str = "hey computer"
    user_name: str = ""
    personality_file: str = "personality/rainbow_dash.md"
    system_prompt_file: str = "personality/system.md"
    acknowledgments_file: str = "personality/acknowledgments.yaml"
    quick_replies_file: str = "personality/quick_replies.yaml"
    # Where the user lives, for the weather, directions and maps when they name no place (core/home.py). "" = not
    # set: those tools ask for a place. The coordinates spare a lookup; without them the place is looked up once.
    home: str = ""
    home_latitude: float | None = Field(None, ge=-90, le=90)
    home_longitude: float | None = Field(None, ge=-180, le=180)

    @field_validator("name", "wake_phrase")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("must not be empty")
        return v


class ConversationConfig(_Section):
    follow_up_window_s: float = Field(8.0, ge=0, description="Listen this long for a follow-up without the wake phrase")
    # While you're on a call in another app (Discord, Zoom, Teams...), no follow-up without her name: what you say there
    # is mostly for the people on it (core/assistant.py ``_follow_up_window``).
    name_on_calls: bool = True
    # While another app plays sound (a video, music, a game), no follow-up without her name, and a wake detector hit
    # counts only once speech recognition hears her name: the headset mic picks up what's playing (``_follow_up_window``,
    # ``hearing._wake_needs_her_name``).
    name_while_playing: bool = True
    session_timeout_s: float = Field(300.0, ge=10, description="Start a fresh conversation after this much inactivity")
    # Once a turn leaves the brain carrying more than this many tokens, it starts afresh between turns (the
    # conversation goes on: its summary and last exchanges are in the fresh prompt). A very long context makes every
    # first word slower: 1.5 to 2.1 s anywhere from 30k to 90k tokens, 2.3 s at 90-120k, 5.6 s past 120k (her turns
    # that read their prompt from the model's cache, 2026-11-22). Starting afresh costs about 6 s of reconnecting and
    # an unread prompt, so the limit in use is never closer than 20k above what a fresh prompt carries, which was 47k
    # by then (``core/session.py``). 0 = never.
    max_context_tokens: int = Field(90000, ge=0)
    wake_ack: Literal["chime", "none"] = "chime"
    # Right after you finish talking, while the answer is being worked out:
    # "speech" = a short cached phrase in the assistant's voice ("On it!"), "chime" = a soft tone.
    acknowledge: Literal["speech", "chime", "none"] = "speech"
    # Answer things like "thanks" from assistant.quick_replies_file without calling the brain.
    quick_replies: bool = True
    confirm_listen_s: float = Field(8.0, gt=0)
    # Say what she's doing between the steps of a task. Off: she works quietly and says only the result, as she does
    # for a conversation once you tell her "just work, talk less" ("talk me through it" brings it back).
    narrate_work: bool = True
    # A turn that gets nowhere for this long (no word from the brain, no tool starting or finishing, nothing said) is
    # stopped, and she says what she was stuck on (``core/watchdog.py``). Her tools and questions get their own time
    # limits on top of it. 0 = never.
    turn_stall_s: float = Field(180.0, ge=0)
    # A soft "mhm" when you pause mid-thought ("set a timer for, um"), so you know she's still listening.
    backchannel: bool = True
    # With headphones: this far into a pause after a request that already sounds finished ("what's the weather"), the
    # acknowledgment ("On it!") plays while the rest of the end-of-speech silence is still listened to; nothing is
    # done until it has passed, so a pause is never taken for the end. 0 = only once the silence is over.
    acknowledge_early_ms: int = Field(600, ge=0)
    # Right as that early acknowledgment plays, the brain starts on the request too, so its first second or so of
    # thinking overlaps the rest of the silence. It runs none of its tools until the silence is over, and the start is
    # dropped if you go on talking (that costs one cut-off Claude call). Needs acknowledge_early_ms.
    early_start: bool = True
    # Her opening words (``brain/opener.py``, docs/octavia-2.0.md): a short first phrase from Haiku about what you
    # said, said while the brain works out the rest, which carries straight on from those exact words. It may only
    # react and name the subject in your words (never an answer, fact, claim or promise). "shadow": it's asked and its
    # verdict logged (routing log ``opener``), nothing said; "on": said; "off": never asked (the original pipeline).
    opener: Literal["off", "shadow", "on"] = "shadow"
    # With "on": how long the early acknowledgment ("On it!") may wait for opening words that could take its place.
    opener_wait_ms: int = Field(500, ge=0, le=2000)


class AudioConfig(_Section):
    input_device: DeviceRef = None  # index or case-insensitive name substring; None = system default
    output_device: DeviceRef = None
    host_api: str | None = None  # None = "Windows WASAPI" on Windows (lowest latency); or "MME", ...
    output_volume: float = Field(1.0, ge=0.0, le=2.0)
    earcons: bool = True
    earcon_volume: float = Field(0.25, ge=0.0, le=1.0)
    sound_pack: Literal["ether", "falcon", "classic"] = "ether"  # soft and far away, sleek and quick, first chimes
    working_sound: bool = True  # a soft loop while she thinks or works on your request (with earcons on)
    # A rising arpeggio when a request of several steps is done. Off: it came at the end of every quiz turn (a panel
    # change and a recorded answer), right after her question, and the user didn't like it (2026-11-22).
    done_sound: bool = False
    headphones: bool = False  # no speaker echo: enables plain voice barge-in


class SherpaKwsConfig(_Section):
    model_dir: str = "models/kws"
    threshold: float = Field(0.25, gt=0, lt=1)  # higher = fewer false wakes, more misses
    boost: float = Field(1.0, gt=0)  # higher = triggers more easily
    num_threads: int = Field(1, ge=1)
    # Other ways to spell the phrase if the model misses an unusual name,
    # e.g. ["hey castrel"] for "hey kestrel". `octavia wake-check` tests this.
    extra_spellings: list[str] = Field(default_factory=list)


class OpenWakeWordConfig(_Section):
    model_path: str = ""  # path to a custom-trained .onnx model for your wake phrase
    threshold: float = Field(0.5, gt=0, lt=1)


class WakeConfig(_Section):
    engine: Literal["sherpa_kws", "openwakeword", "keyboard"] = "sherpa_kws"
    cooldown_s: float = Field(1.5, ge=0)
    # A second listen (``wake/second_chance.py``): speech heard while waiting for the wake word that the detector
    # didn't fire on is transcribed, and wakes her when it starts with her name; what follows the name is the request.
    second_chance: bool = True
    second_chance_max_s: float = Field(8.0, gt=1, le=30)  # longer stretches of speech are someone talking, not calling
    sherpa_kws: SherpaKwsConfig = Field(default_factory=SherpaKwsConfig)
    openwakeword: OpenWakeWordConfig = Field(default_factory=OpenWakeWordConfig)


class VadConfig(_Section):
    model_path: str = "models/silero_vad.onnx"
    threshold: float = Field(0.5, gt=0, lt=1)
    neg_threshold: float = Field(0.35, gt=0, lt=1)
    min_speech_ms: int = Field(120, ge=0)
    end_silence_ms: int = Field(700, ge=100)
    pre_roll_ms: int = Field(400, ge=0)
    max_utterance_s: float = Field(30.0, gt=1)
    no_speech_timeout_s: float = Field(6.0, gt=0)
    # While you pause, what you've said so far is transcribed already (``octavia.stt.early``): this far into a pause
    # (0 = off, and the transcript waits for the end of the utterance).
    early_transcript_ms: int = Field(300, ge=0)
    # Smart Turn (``octavia.vad.smart_turn``): in each pause of a request, whether you sounded finished, from your voice.
    # Shadow only: its verdicts go to the routing log (`octavia routing`) and change nothing. "" = off.
    smart_turn_model: str = "models/smart_turn/smart-turn-v3.2-cpu.onnx"

    @model_validator(mode="after")
    def _check(self) -> VadConfig:
        if self.neg_threshold >= self.threshold:
            raise ValueError("vad.neg_threshold must be lower than vad.threshold")
        return self


class SttConfig(_Section):
    engine: Literal["parakeet"] = "parakeet"
    # onnx-asr model name and its Hugging Face repo. For other languages use
    # "nemo-parakeet-tdt-0.6b-v3" / "istupakov/parakeet-tdt-0.6b-v3-onnx".
    model: str = "nemo-parakeet-tdt-0.6b-v2"
    hf_repo: str = "istupakov/parakeet-tdt-0.6b-v2-onnx"
    quantization: Literal["int8"] | None = "int8"
    # null = models/stt/<repo name without "-onnx">, so changing hf_repo also changes the folder.
    model_dir: str | None = None
    num_threads: int = Field(8, ge=1)
    language: str = "en"

    def resolved_model_dir(self) -> str:
        if self.model_dir:
            return self.model_dir
        name = self.hf_repo.rstrip("/").split("/")[-1]
        return f"models/stt/{name.removesuffix('-onnx')}"


class BargeInConfig(_Section):
    enabled: bool = True
    wake_phrase: bool = True  # saying the wake phrase while it talks interrupts it
    # Interrupt on any sustained speech. None = automatic: on with headphones; on speakers, on while echo
    # cancellation is running and measured removing her voice well. True = on speakers whenever echo cancellation runs.
    voice: bool | None = None
    echo_cancellation: bool = True  # WebRTC AEC3 via the `livekit` package, if installed
    vad_threshold: float = Field(0.7, gt=0, lt=1)  # stricter than normal listening
    min_speech_ms: int = Field(256, ge=32)  # sustained speech needed to interrupt
    aec_warmup_s: float = Field(3.0, ge=0)  # ignore voice barge-in while the echo canceller converges


class ClaudeConfig(_Section):
    model: str = "opus"  # alias (opus / sonnet / haiku) or a full model id
    fallback_model: str | None = "sonnet"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"  # lower = faster replies
    # After a restart or a fresh start, one tiny request with her prompt and tools so the user's next turn reads
    # them from the model's cache (within the usage budget; skipped when this prompt was read within the hour).
    warm_cache: bool = True
    # Steps (tool round trips) per request: room for a long multi-step task, like working through a problem set on
    # screen. At the limit she pauses and says so; "keep going" carries on.
    max_turns: int = Field(80, ge=1)
    # Read-only Claude Code built-ins to enable (web search makes weather/news questions work).
    # These run inside Claude Code, outside this project's permission policy, so only the
    # read-only web tools are accepted unless allow_unrestricted_builtins is set.
    builtin_tools: list[str] = Field(default_factory=lambda: ["WebSearch", "WebFetch"])
    allow_unrestricted_builtins: bool = False  # DANGEROUS: e.g. "Bash" would run commands unconfirmed
    # Tool schemas were most of every request (about 45k of 55k tokens). On demand, only always_loaded_tools go out
    # in full; the rest are listed by name, and Claude loads the ones a request needs with Claude Code's
    # ToolSearch (one short extra step the first time in a conversation). False = every schema in every request.
    tools_on_demand: bool = True
    # Sent in full every request when tools_on_demand: the tools most turns use, and the screen-driving ones whose
    # loops are the slowest. Names or patterns ("canvas_*").
    always_loaded_tools: list[str] = Field(default_factory=lambda: list(ALWAYS_LOADED_TOOLS))
    auth: Literal["subscription", "api_key"] = "subscription"
    connect_timeout_s: float = Field(90.0, gt=0)
    # Mid-turn silence from Claude (no streamed word, thinking or tool call, and no tool running) after which she
    # gives up on the turn and reconnects, instead of thinking forever.
    stall_timeout_s: float = Field(120.0, gt=0)
    cli_path: str | None = None  # default: the CLI bundled with claude-agent-sdk
    # Private Claude Code config dir (login + transcripts), separate from your own Claude Code.
    # null = share ~/.claude with your normal Claude Code install.
    config_dir: str | None = "data/claude-home"
    resume_last_session: bool = False  # continue the previous conversation after a restart
    work_dir: str = "data/brain"

    @model_validator(mode="after")
    def _check_builtins(self) -> ClaudeConfig:
        unsafe = [t for t in self.builtin_tools if t and t not in READ_ONLY_BUILTINS]
        if unsafe and not self.allow_unrestricted_builtins:
            raise ValueError(
                f"brain.claude.builtin_tools may only contain {sorted(READ_ONLY_BUILTINS)}; {unsafe} would run "
                "outside the permission policy with no confirmation. Write a Tool instead, or set "
                "allow_unrestricted_builtins: true if you accept that."
            )
        return self


class OpenAICompatibleConfig(_Section):
    """Her brain on any OpenAI-compatible chat API (``brain/openai_compat.py``) when ``brain.provider`` is
    ``openai_compatible``: OpenAI, Gemini, Grok, Mistral, DeepSeek, Groq, OpenRouter, or a local server (Ollama,
    LM Studio, llama.cpp). What only Claude has (its web tools, prompt cache warm-up, her quick voice) is off there."""

    # e.g. https://api.openai.com/v1, https://generativelanguage.googleapis.com/v1beta/openai, https://api.x.ai/v1,
    # https://api.mistral.ai/v1, https://api.deepseek.com/v1, https://api.groq.com/openai/v1,
    # https://openrouter.ai/api/v1, http://localhost:11434/v1 (Ollama), http://localhost:1234/v1 (LM Studio)
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"  # the environment variable (.env) holding the key; "" for a server with none
    model: str = ""  # the model's name on that server, e.g. gpt-5.1, gemini-3-pro, grok-4, qwen3:14b
    max_turns: int = Field(80, ge=1)  # steps (tool round trips) per request, as brain.claude.max_turns
    vision: bool = True  # send pictures (the screen, what was circled); off for a model that can't see
    temperature: float | None = Field(None, ge=0, le=2)  # null = the server's default
    max_tokens: int | None = Field(None, ge=1)  # per reply; null = the server's default
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None  # for reasoning models that take it
    # More tools than this in one request are refused by most of these APIs (OpenAI: 128). Her ~250 are more, so
    # with tools_on_demand always_loaded_tools go in full and the rest are listed by name in a load_tools tool that
    # loads them when a request needs them.
    max_tools: int = Field(128, ge=2)
    tools_on_demand: bool = True
    always_loaded_tools: list[str] | None = None  # null = brain.claude.always_loaded_tools
    # The server said the conversation grew past this: the oldest exchanges are dropped, whole.
    context_limit_tokens: int = Field(90000, ge=4000)
    stream_usage: bool = True  # ask for token counts with the stream (stream_options); off for a server refusing it
    connect_timeout_s: float = Field(30.0, gt=0)
    request_timeout_s: float = Field(300.0, gt=0)
    stall_timeout_s: float = Field(120.0, gt=0)  # silent this long mid-reply: give up on the turn
    extra_headers: dict[str, str] = Field(default_factory=dict)  # e.g. OpenRouter's HTTP-Referer / X-Title
    judge_model: str = ""  # the eval's judge on this server when there's no Claude; "" = model


class BrainConfig(_Section):
    # "claude" (Claude Code, your subscription) | "openai_compatible" (any OpenAI-style chat API: brain.openai_compatible)
    provider: Literal["claude", "openai_compatible"] = "claude"
    # Who answers your turns (``core/engines.py``): "auto", the local brain's routing as ``local_brain.mode`` sets it;
    # "claude", only Claude (the local brain never answers, not even while Claude is down); "local", only the local
    # brain (it says plainly what it can't do; it never hands a turn to Claude). Changed live.
    answers: Literal["auto", "claude", "local"] = "auto"
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    openai_compatible: OpenAICompatibleConfig = Field(default_factory=OpenAICompatibleConfig)

    @property
    def is_claude(self) -> bool:
        return self.provider == "claude"

    @property
    def model(self) -> str:
        """The conversation's model, whichever provider runs it."""
        return self.claude.model if self.is_claude else self.openai_compatible.model


class FishAudioConfig(_Section):
    api_key_env: str = "FISH_AUDIO_API_KEY"
    reference_id: str = ""  # voice model id from fish.audio (32 hex chars); or set FISH_AUDIO_VOICE_ID
    reference_id_env: str = "FISH_AUDIO_VOICE_ID"
    model: str = "s2.1-pro-free"  # free fair-use tier; "s2.1-pro" is the same model, paid (see README)
    # http: the first audio of a sentence came 0.63 s after it was asked for, against 1.00 s over the websocket
    # (2026-11-22, ``tts/fish_audio.py``).
    transport: Literal["websocket", "http"] = "http"
    sample_rate: int = 44100
    latency: Literal["low", "balanced", "normal"] = "balanced"
    chunk_length: int = Field(100, ge=100, le=300)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    temperature: float | None = Field(None, ge=0, le=1)
    top_p: float | None = Field(None, ge=0, le=1)
    expressive_tags: bool = True  # let the model use S2 tone cues like [excited]
    spare_socket_max_idle_s: float = Field(30.0, gt=0)
    base_url: str = "https://api.fish.audio"


class LocalVoiceConfig(_Section):
    # Her cloned voice made on this PC (docs/octavia-architecture.md, "Local voice"): Qwen3-TTS 1.7B run by a worker in
    # the Python of a Qwen3-TTS-GGUF checkout (github.com/HaujetZhao/Qwen3-TTS-GGUF).
    python: str = ""  # that checkout's python.exe
    engine_dir: str = ""  # the checkout (its qwen3_tts_gguf package, with llama.cpp's Vulkan DLLs in inference/bin)
    model_dir: str = ""  # the converted model folder (model-base: talker/predictor GGUF, decoder ONNX)
    voice: str = "data/voices/octavia.wav"  # clean clips of her voice to clone (15 to 30 s)
    voice_text: str = "data/voices/octavia.txt"  # exactly what's said in them
    gpu: bool = True  # the talker on llama.cpp Vulkan, the decoder on DirectML; off: all on the CPU (too slow to talk)
    first_frames: int = Field(5, ge=1, le=12)  # decoded this often until sound comes out (0.3 s to first audio at 5)
    # Steadiness: each sentence is made on its own, so randomness shows as her tone changing from one to the next
    # (the user heard it at 0.9/0.9). Low randomness and one seed for every sentence halved the pitch drift between
    # sentences (0.47 vs 0.77 semitones, 2026-10-26) with the same voice match.
    temperature: float = Field(0.3, gt=0, le=1.5)  # how freely each sentence's delivery is picked
    detail_temperature: float = Field(0.1, gt=0, le=1.5)  # the same for the fine acoustic detail
    min_p: float = Field(0.05, ge=0, lt=1)  # leaves out unlikely sounds (less buzz)
    seed: int = Field(42, ge=-1)  # every sentence sampled the same way; -1 = a new draw each time
    language: str = "english"
    start_timeout_s: float = Field(180.0, gt=0)  # a first start clones the voice (20 to 45 s)
    game_unload_s: float = Field(10.0, ge=0)  # a game full screen this long: unloaded, Fish speaks (0 = never)
    game_reload_s: float = Field(120.0, ge=0)  # loaded again once no game has been in front this long


class TTSConfig(_Section):
    # "fish_audio", or "local": her cloned voice made on this PC, with Fish Audio as its backup; "local_only": that
    # voice without Fish behind it. Started local (either way), the three switch live (``core/engines.py``).
    provider: Literal["fish_audio", "local", "local_only"] = "fish_audio"
    fish_audio: FishAudioConfig = Field(default_factory=FishAudioConfig)
    local: LocalVoiceConfig = Field(default_factory=LocalVoiceConfig)
    # The text sent to Fish in one go. Small is smoother: Fish's audio for a piece comes in growing chunks, so a
    # long piece runs dry mid-sentence when Fish is slow (measured 2026-10-25: 7 pauses at 220, 2 at 40).
    min_chunk_chars: int = Field(40, ge=1, description="Send text to TTS in pieces of at least this many chars")
    # A backup voice for when Fish Audio fails: "windows" speaks with a built-in Windows voice, "none" stays silent.
    fallback: Literal["windows", "none"] = "windows"
    windows_voice: str = ""  # part of a Windows voice's name (e.g. "Zira"); "" = the first female English one
    fallback_after_s: float = Field(5.0, gt=0)  # no sound from Fish this long into a sentence: the backup says it
    fallback_cooldown_s: float = Field(120.0, gt=0)  # after Fish fails, use the backup this long before retrying it
    # An effect on her voice, on either engine (``audio/voice_effects.py``): "off", or a preset. Switched live by
    # voice ("use your radio voice"), the tray and her settings window (``core/engines.py``).
    voice_effect: Literal["off", "radio", "console", "robot", "room", "deeper"] = "off"


class ToolsConfig(_Section):
    enabled: list[str] = Field(default_factory=lambda: ["*"])
    disabled: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)  # extra modules exposing TOOLS or register(registry)
    # Spoken name -> installed app name, or an allow-listed URI (e.g. "ms-settings:bluetooth").
    app_aliases: dict[str, str] = Field(
        default_factory=lambda: {
            "vs code": "Visual Studio Code",
            "vscode": "Visual Studio Code",
            "settings": "ms-settings:",
            "bluetooth settings": "ms-settings:bluetooth",
            "sound settings": "ms-settings:sound",
        }
    )
    app_index_ttl_h: float = Field(6.0, gt=0)
    extra_uri_prefixes: list[str] = Field(default_factory=list)  # more URI schemes open_application may launch
    search_engine: str = "google"


class GodsEyeConfig(_Section):
    """God's Eye View (github.com/bilawalsidhu/gods-eye-view), the 3D globe she shows places on. Its optional keys
    (CESIUM_ION_TOKEN, GOOGLE_MAPS_API_KEY) are in .env like her others."""

    enabled: bool = True  # "show me ..." opens it; off: the satellite picture window and Google Maps as before
    folder: str = ""  # its checkout; empty: gods-eye-view next to her own folder
    address: str = "http://localhost:4173"  # where its local server answers; she starts it when it doesn't


class VisualsConfig(_Section):
    """Her pop-up panels (diagrams, charts, formatted notes) shown while she explains something; docs/visuals.md."""

    enabled: bool = True  # she can pop up panels (show_visual); off: she explains out loud and with show_note only
    stay_s: float = Field(
        default=180.0, ge=0, le=3600
    )  # how long a panel stays (paused under the pointer); 0: until closed
    max_panels: int = Field(default=3, ge=1, le=6)  # on screen at once; a new one closes the oldest past this
    hide_from_capture: bool = True  # out of screenshots (hers included), recordings and screen shares
    idle_quit_s: float = Field(default=600.0, ge=0)  # the panel app quits after this long with no panel up; 0: never
    # Her status panel of what runs beside her (downloads, tasks, builds, objectives): "auto" comes up by itself while
    # something runs, "ask" only for "what's running?", "off" never (she still says it).
    status_panel: Literal["auto", "ask", "off"] = "auto"
    # She can draw arrows, rings, boxes, highlights and labels over your screen to guide you (mark_screen,
    # guide_on_screen; docs/screen-marks.md). Your stop hotkey clears them.
    marks: bool = True
    marks_stay_s: float = Field(default=90.0, ge=0, le=3600)  # how long marks stay up; 0: until cleared
    # Pictures from the web on a panel ("show me Sora from Kingdom Hearts": show_image) instead of image results in
    # the browser, and web pages shown live on a panel in a sandbox of their own (show_page; docs/visuals.md).
    web_images: bool = True
    web_pages: bool = True
    image_stay_s: float = Field(default=120.0, ge=0, le=3600)  # how long a picture stays; 0: until closed
    page_stay_s: float = Field(default=600.0, ge=0, le=3600)  # how long a web page stays; 0: until closed
    safe_search: Literal["strict", "moderate", "off"] = "moderate"  # the image search's filter for adult pictures
    # Explainers (explain: a panel that plays step by step as she talks): her orb flies over and points at what each
    # step is about, as well as the pointer on the panel.
    explainer_orb_points: bool = True
    # Closed panels kept so they can be brought back ("bring back that diagram", her chat window, her tray), and for how
    # long (data/visuals/shelf.json on this PC). Off: only pinned ones are kept.
    shelf: bool = True
    shelf_days: float = Field(default=14.0, ge=1, le=90)


class HotkeysConfig(_Section):
    enabled: bool = True
    # Global shortcuts (RegisterHotKey; no keyboard hook, no admin). "" disables one.
    talk: str = "ctrl+alt+j"  # start listening now (or interrupt and listen)
    stop: str = "ctrl+alt+k"  # stop talking / cancel
    mute: str = "ctrl+alt+m"  # mute or unmute her listening (as the tray's Mute listening)
    chat: str = "ctrl+alt+o"  # open her chat window: today's conversation, and a box to type to her


class PermissionsConfig(_Section):
    safe: Decision = Decision.ALLOW
    confirm: Decision = Decision.CONFIRM
    restricted: Decision = Decision.DENY
    tools: dict[str, Decision] = Field(default_factory=dict)
    confirm_timeout_s: float = Field(30.0, gt=0)
    # Sites where she submits and sends without asking while you're working with her: words of the window's title,
    # e.g. ["DataCamp"]. Only submitting and sending: buying, deleting, installing, passwords and commands still ask.
    submit_without_asking: list[str] = Field(default_factory=list)
    # Apps where she deletes things inside your document without asking (a shape, a slide, a cell: Ctrl+Z takes it
    # back), e.g. ["PowerPoint"]. Their dialogs ("Don't Save") and everything else risky still ask.
    trusted_apps: list[str] = Field(default_factory=list)
    # High-risk places: submitting or sending there is asked every single time, by a word of the window's
    # title. No "you don't need to ask", earlier yes or submit_without_asking skips it. Email by default;
    # add e.g. "Canvas" so turning in coursework always asks.
    always_ask_before_sending: list[str] = Field(
        default_factory=lambda: ["Gmail", "Outlook", "Proton Mail", "Yahoo Mail", "Thunderbird"]
    )

    @model_validator(mode="after")
    def _check(self) -> PermissionsConfig:
        if self.restricted in (Decision.ALLOW, Decision.AUTO):
            raise ValueError(
                f"permissions.restricted cannot be '{self.restricted.value}'. Allow individual restricted tools "
                "under permissions.tools"
            )
        return self


class ClaimsConfig(_Section):
    """What she says she did, checked against her tools, and what she says she'll do, kept (``core/claims.py``)."""

    # enforce: a claim her tools contradict ("I sent it" when nothing confirmed the send) is said honestly instead,
    # and she's told on her next turn; note: only told to the brain's next turn; log: only written to ``log_file``;
    # off.
    mode: Literal["off", "log", "note", "enforce"] = "enforce"
    log_file: str = "data/claims.jsonl"
    promises_file: str = "data/promises.json"


class MemoryConfig(_Section):
    enabled: bool = True
    database: str = "data/octavia.db"
    # Every current note fits (88 on 2026-10-25, ~2.4k tokens); the pick only matters past this.
    max_prompt_items: int = Field(150, ge=0)


class SystemConfig(_Section):
    single_instance: bool = True
    disable_power_throttling: bool = True  # keep Windows EcoQoS from slowing wake-word detection
    tray_icon: bool = True  # her icon by the clock: right-click it to shut her down, restart or mute her
    # Restart into commits made outside her (say, with Claude Code) once she's idle (octavia/new_code.py)
    restart_for_new_code: bool = True
    new_code_quiet_s: float = Field(180.0, ge=10, le=3600)  # how long HEAD stays put before she restarts into it
    # Her submenu in Explorer's right-click menu, on files, folders and the desktop (platform/explorer_menu.py)
    explorer_menu: bool = False
    # A scheduled task that starts her again if her supervisor dies (platform/revive_task.py, evolve/revive.py)
    revive: bool = True


class SelfImprovementConfig(_Section):
    """The assistant fixing and extending its own code (see docs/self-improvement.md)."""

    enabled: bool = False
    # The coding agent that writes changes (a separate Claude Code session in a git worktree).
    builder_model: str = "opus"
    builder_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    builder_max_turns: int = Field(150, ge=5)
    build_timeout_min: float = Field(30.0, gt=0)
    test_timeout_min: float = Field(10.0, gt=0)
    max_builds_per_day: int = Field(5, ge=0)  # each build uses Claude usage
    # How many builds run at once (each uses Claude usage at the same time); 1 = one at a time.
    parallel_builds: int = Field(2, ge=1, le=8)
    retry_failed_checks: bool = True  # one more round when the final tests fail
    # off = build only when you ask; errors = also fix crashes in its own code by itself;
    # all = also build every missing feature it notes (uses the most Claude usage).
    auto_build: Literal["off", "errors", "all"] = "off"
    # never = always ask before installing; safe_changes = install on its own when the checks pass
    # and nothing sensitive changed (core files, edited tests, new tools that need no confirmation).
    auto_install: Literal["never", "safe_changes"] = "never"
    announce: Literal["speak", "console"] = "speak"  # tell you out loud when a build is ready
    notice_errors: bool = True  # record exceptions in its own code as things to fix
    extra_protected_paths: list[str] = Field(default_factory=list)  # globs the builder may never change
    # Her night shift (evolve/nightly.py): at nightly_at she reads the day's turns, starts fixes for the worst
    # patterns (at most nightly_builds) and runs her real-request check. Until morning_report, results are shown,
    # not said; the first conversation after it hears about the night. "HH:MM"; nightly_at "" = never.
    nightly_at: str = "03:00"
    morning_report: str = "09:00"
    nightly_builds: int = Field(3, ge=0, le=10)
    # Once a week the night shift asks her real past failures again (evolve/frontier.py): a held-out half as the
    # growth curve in the morning report, the other half's misses as fixes to build. About 2 % of a week's usage.
    frontier_weekly: bool = True

    @field_validator("nightly_at", "morning_report")
    @classmethod
    def _clock_time(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("self_improvement.nightly_at and morning_report must be times like 03:00 (or empty)")
        return value

    @model_validator(mode="after")
    def _a_night(self) -> SelfImprovementConfig:
        if self.nightly_at and self.nightly_at == self.morning_report:
            raise ValueError("self_improvement.nightly_at and morning_report can't be the same time")
        return self

    @field_validator("auto_build", mode="before")
    @classmethod
    def _unquoted_off(cls, v: object) -> object:
        # YAML 1.1 reads a bare `off` as False. True stays an error: `on` or `yes` could mean either level.
        return "off" if v is False else v


class AvatarPropsConfig(_Section):
    """What the character holds for each activity: a prop's name (`octavia avatar props` lists them), "" = nothing."""

    camera: str = "camera"  # while taking a screenshot
    search: str = "magnifier"  # while looking closely at your screen or searching (memory, calendar...)
    tasks: str = "clipboard"  # while working with your TickTick tasks, and ticking one off
    timer: str = "stopwatch"  # setting a timer (or a focus session), and when one goes off
    reminder: str = "bell"  # rung as she reminds you of a task
    web: str = "globe"  # spun while she searches or reads the web
    files: str = "folder"  # flipped through while she looks through your files
    morning: str = "mug"  # sipped through the morning rundown
    build: str = "wrench"  # twirled as a build of her own starts
    music: str = "headphones"  # worn for a beat when she starts your music
    shout: str = "megaphone"  # for a shout in her reply
    rain: str = "umbrella"  # when she tells you it's raining (or snowing)
    sun: str = "sunglasses"  # when she tells you it's sunny

    @field_validator("*", mode="before")
    @classmethod
    def _none_is_nothing(cls, v: object) -> object:
        # `camera: null`, or YAML 1.1's bare `no`/`off` (read as False), means no prop, like "".
        return "" if v is None or v is False else v

    @field_validator("*")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return v.strip().lower()


class AvatarConfig(_Section):
    """The 3D character on the desktop (see docs/avatar.md)."""

    enabled: bool = False
    model: str = "models/avatar/octavia.vrm"  # a VRM file; models/ is git-ignored
    # conversation = pops in when you talk to it and leaves once it's been idle; always = stays on screen.
    mode: Literal["conversation", "always"] = "conversation"
    # Idle this long before it leaves, when not back to waiting for the wake word (then it leaves at once).
    hide_after_s: float = Field(20.0, ge=0)
    height: int = Field(560, ge=200, le=2000)  # window height in pixels
    framing: Literal["full", "upper"] = "full"  # whole body, or waist up
    lip_sync: bool = True
    click_to_talk: bool = True  # clicking the character starts listening, like the wake word
    credit: str = "my oc by amia"  # who made the character (its license asks for credit)
    # Keep her out of screenshots (the assistant's own look at your screen), recordings and screen shares.
    hide_from_capture: bool = True
    electron: str = ""  # the Electron executable; empty = avatar/node_modules (from `octavia avatar setup`)
    # She can fly over to something on your screen and point at it (the point_at_screen tool).
    point: bool = True
    point_height: int = Field(0, ge=0, le=2000)  # her height in pixels while pointing (whole body); 0 = height
    # Once the reply is over she keeps pointing this long, the ring fading as it runs out, then flies home.
    point_linger_s: float = Field(5.0, ge=0, le=90)
    trail: bool = True  # a rainbow streak across the screen when she flies
    props_dir: str = "models/avatar/props"  # things she can hold: .glb files, with optional .json settings
    props: AvatarPropsConfig = Field(default_factory=AvatarPropsConfig)
    hover: bool = False  # she floats while you talk to her: legs tucked, a little off her spot, still
    # Left idle on screen this long (`always` mode) she dozes off on a little cloud; after a break this long she's
    # caught napping when she comes back, and wakes right up. 0 = never.
    nap_after_min: float = Field(30.0, ge=0, le=24 * 60)
    # A sonic rainboom when something big is done: an improvement installed, or a long task that all worked.
    rainboom: bool = True
    # A victory lap round the screen when you tick off the last of what's due today in TickTick.
    victory_lap: bool = True
    # She glances at the window you switch to (the window in front, looked at twice a second on this PC).
    glance: bool = True
    # In always mode, while a build of hers runs and nothing else is going on, she sits on top of the window you're
    # working in (when it has room above it), and flies home when you talk to her, switch windows or the build ends.
    perch: bool = True
    # She jumps at a sudden bang in the room (the mic's loudness only, while waiting for the wake word) or a big window
    # popping up by itself.
    startle: bool = True
    # How she looks: "character" (the VRM at `model`) or "orb" (a glowing orb drawn by the app, no model needed).
    form: Literal["character", "orb"] = "character"
    orb_size: int = Field(320, ge=96, le=600)  # the orb's square window in pixels, at home and while pointing
    # Subtitles beside her as she speaks, a sentence at a time, showing what she means: math as math, code and figures
    # as written (docs/subtitles.md).
    subtitles: bool = True

    @field_validator("point_height")
    @classmethod
    def _point_height(cls, v: int) -> int:
        if 0 < v < 200:  # same lower limit as height: smaller and there's nothing left to see
            raise ValueError("must be 0 (same as height) or at least 200")
        return v


class InnerLifeConfig(_Section):
    """Her inner life (docs/inner-life.md): each night she thinks the day over on her own, with read-only tools, and
    keeps a journal (thoughts, questions, follow-ups, opinions); the next time you talk she may bring one thing up.
    It runs on your Claude subscription (one background session a night, none on a day with nothing new), keeps its
    journal in a database of its own, and follows Futures' privacy controls."""

    enabled: bool = True
    database: str = "data/inner/inner.db"
    reflect_at: str = "03:30"  # local "HH:MM": the nightly reflection (any time in the 4 hours after, if the PC slept)
    # No reflection for this long (the PC was off at night): she catches up once you've stepped away for a while.
    catch_up_hours: float = Field(20.0, ge=4, le=24 * 7)
    idle_minutes: float = Field(10.0, ge=1, le=240)
    max_per_day: int = Field(2, ge=1, le=6)  # reflections that ran (skipped ones don't count)
    bring_up: bool = True  # mention one thought early in the next conversation, when it fits
    # A mood of her own from how the day went (her builds, how you've sounded, how much you've talked), counted on
    # this PC (``inner/mood.py``): it colors her tone, and she says it only when you ask how she is.
    mood: bool = True
    # A growing self (docs/inner-life.md): notes of her own about herself that she writes, rewrites and takes back
    # without asking (in every prompt; checked in code, never a rule for herself), a monthly look back on who she was,
    # and the odd dream. What happened to her and opinions that change only after a second night are always kept.
    self_notes: bool = True
    looking_back: bool = True
    dreams: bool = True
    # May she search the web while reflecting? Off by default: the night's material is private, and a page she
    # reads could try to send it somewhere through the next fetch.
    web: bool = False
    digest_chars: int = Field(24000, ge=2000, le=120000)  # how much of the day's conversations she reads
    time_limit_min: float = Field(15.0, ge=1, le=60)

    @field_validator("reflect_at")
    @classmethod
    def _clock(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("inner_life.reflect_at must look like 03:30")
        return value


class RoutinesConfig(_Section):
    """Learning by watching (docs/routines.md): the routines she notices in what she already records (which app you
    open when, which app follows which, what you ask her for at the same time of day), offered back as a question at
    the right moment, through the same gate as everything she says on her own. Counting on this PC only: no Claude
    usage, no new capture, and she never acts on a routine by herself."""

    enabled: bool = True  # learn routines at all
    offers: bool = True  # offer them ("You usually open Spotify around now. Want me to open it?")
    database: str = "data/routines/routines.db"
    mine_at: str = "04:15"  # local "HH:MM": when she looks for routines each day (and at start, if a day has passed)
    history_days: int = Field(28, ge=7, le=120)  # how far back she looks
    min_days: int = Field(4, ge=3, le=30)  # a routine happened on at least this many days...
    min_support: float = Field(0.5, ge=0.3, le=1.0)  # ...and on at least this share of the days it could have
    offers_per_day: int = Field(1, ge=1, le=3)
    exclude_apps: list[str] = Field(default_factory=list)  # never learned or offered (screen history's list counts too)

    @field_validator("mine_at")
    @classmethod
    def _clock(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("routines.mine_at must look like 04:15")
        return value


class StudyConfig(_Section):
    """Studying with her (docs/study.md): the study tracker (each course's next exam, its topics, how far you've got,
    weak spots and the questions you missed), exams noticed in what Canvas lists, and the few things she brings up
    on her own about them, through the same gate as all her remarks."""

    enabled: bool = True
    nudges: bool = True  # ask about studying when an exam is close, and offer reviews of missed questions
    exam_days: int = Field(3, ge=1, le=14)  # an exam this many days away or fewer, with no study lately, is asked about
    min_reviews: int = Field(3, ge=1, le=20)  # missed questions due before she offers a review
    canvas_exams: bool = True  # look for exams (Midterm, Class Test 2, Final) in Canvas's planner every few hours


class UsageBudgetConfig(_Section):
    """How much of your Claude subscription her own background work may use (core/usage.py): the night shift, the
    builds she starts by herself, her real-request check and her nightly reflection. Every Claude call reports how
    full your 5-hour and weekly limits are (the same numbers Claude Code's /usage shows); she checks them before each
    of those jobs and skips it when its share is spent or your limits are filling up. What you ask her yourself is
    never held back. `octavia usage` (or asking her) shows where things stand and what she skipped."""

    enabled: bool = True
    # Her background work's share per day, as a fraction of your WEEKLY limit (0.05 = 5% a day, 35% a week at most).
    # 0 = no background Claude work at all.
    daily_share: float = Field(0.05, ge=0, le=1)
    # Background work waits while your 5-hour window is at least this full, so you have room to talk to her.
    max_five_hour: float = Field(0.6, ge=0, le=1)
    # ...or while your weekly limit is at least this full.
    max_weekly: float = Field(0.8, ge=0, le=1)


class ObjectivesConfig(_Section):
    """Objectives (docs/objectives.md): an outcome you hand her that she plans, splits into steps, runs beside the
    conversation (research, and code on a branch of one of your projects), has checked independently and reports on
    once. Voice mode only."""

    enabled: bool = True
    # Folders whose git repositories she may code in (each direct subfolder that is a repository); her own checkout is
    # always left out. Empty = the folder her own checkout is in.
    project_roots: list[str] = Field(default_factory=list)
    # The command that checks a project, by folder name, when the one she finds isn't right: {"mygame": "cargo test"}.
    checks: dict[str, str] = Field(default_factory=dict)
    max_open: int = Field(3, ge=1, le=10)  # objectives under way at once
    research_parallel: int = Field(2, ge=1, le=4)  # research steps at once (code steps always go one at a time)
    # The planner and code steps (Claude Code sessions, like her self-improvement builds).
    model: str = "opus"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # The reviewer that checks each step and didn't do it.
    review_model: str = "opus"
    review_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    code_timeout_min: float = Field(45.0, gt=0)
    check_timeout_min: float = Field(20.0, gt=0)  # one run of a project's tests or build
    web: bool = True  # the planner and research reviewers may search and read the web
    # Hold steps back while your Claude limits are filling up (usage_budget's max_five_hour and max_weekly). Today's
    # share for her own background work doesn't apply: this is work you asked for.
    respect_limits: bool = True


class ProactiveConfig(_Section):
    """Work she does before you ask (docs/proactive.md): at night, while you're away, she picks a few things worth
    preparing from what she knows of your week (Canvas, the study tracker, TickTick, your calendar, your projects and
    what you've told her), runs them as objectives so each result is checked by a reviewer that didn't do it, and in
    the morning hands you what's ready to approve, reject or redirect. Prepared work only: drafts, outlines, study
    sets, notes and code on a branch. She never sends, submits, buys, deletes, calls or texts on her own."""

    # off; suggest: plan only, and ask in the morning before doing any of it; drafts: prepare read-only work (study
    # sets, outlines, briefs) overnight; projects: also code on a branch of the projects listed below.
    level: Literal["off", "suggest", "drafts", "projects"] = "drafts"
    max_items: int = Field(2, ge=1, le=6)  # things prepared a night
    night: str = "01:00-08:00"  # local "HH:MM-HH:MM": when she may plan and work (only once you've been away)
    away_min: float = Field(30.0, ge=5, le=240)  # no turn of yours this long first
    look_ahead_days: int = Field(4, ge=1, le=14)  # deadlines, exams and meetings this close are worth preparing for
    rank: bool = True  # one cheap model call ranks the candidates against what she knows (off: rules only)
    rank_model: str = "sonnet"
    rank_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    # Folder names of projects she may code in on her own at the "projects" level (each also under
    # objectives.project_roots). Empty: none.
    projects: list[str] = Field(default_factory=list)
    # Her guesses at what you'd pick only shape the plan once she has this many of your decisions and has guessed
    # them right this often.
    steer_min_decisions: int = Field(10, ge=3, le=200)
    steer_agreement: float = Field(0.7, ge=0.5, le=1.0)
    folder: str = "data/proactive"

    @field_validator("night")
    @classmethod
    def _window(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d-(?:[01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("proactive.night must look like 01:00-08:00")
        return value


class HeadsUpConfig(_Section):
    """Heads-ups (``core/heads_up.py``): new or moved work in Canvas and TickTick, and new email, judged by her and
    brought up in her own words only when it matters to you now; an urgent one goes to your phone too. Voice mode
    only. Off: email is said as before (sender and subject), and Canvas and TickTick changes only by Futures."""

    enabled: bool = True
    model: str = "sonnet"  # judges each batch: one short call with no tools, on her own share of the usage
    settle_s: float = Field(45.0, ge=0, le=600)  # what arrives within this is judged together


class FinanceConfig(_Section):
    """Money (``octavia/finance``, docs/finance.md): your transactions, balances, recurring payments, income, loans and
    credit score, from the statements you drop in the inbox and the bank pages you signed in to in her bank
    browser. Read-only and kept on this PC; she never moves money. What's new and matters comes up as a heads-up."""

    enabled: bool = True
    inbox: str = "data/finance/inbox"  # statements (CSV, OFX, QFX) dropped here are imported
    reads_per_day: int = Field(3, ge=0, le=12)  # bank pages read this often (0: only when you ask)
    heads_ups: bool = True  # new facts (a price going up, money short before payday) go to her heads-ups
    low_balance: float = Field(100.0, ge=0, le=100_000)  # money left before payday below this is worth a word


class DeadlinesConfig(_Section):
    """Deadline alerts (``core/deadlines.py``): Canvas work still open and calendar events, brought up as they get
    close, each stage once and a little louder each time; the last one breaks through focus mode and goes to your
    phone too. Quiet hours hold them unless you're at the PC. Voice mode only. Off: Futures offers help as before."""

    enabled: bool = True
    # Hours before Canvas work is due to bring it up, furthest first: the last is the urgent last call.
    canvas_h: list[float] = Field(default_factory=lambda: [24.0, 3.0, 0.75], min_length=1, max_length=5)
    event_min: float = Field(15.0, ge=0, le=180)  # minutes before a calendar event to say it's starting (0: never)


class UnpromptedConfig(_Section):
    """The one judgment everything she says on her own passes first (``core/unprompted.py``): what you waved off
    ("shut up about that") holds for every part of her, she doesn't repeat herself, and what's gone stale or would
    intrude waits. What you asked for (reminders, timers) is always said. Off: everything is said as it comes."""

    enabled: bool = True
    model: str = "sonnet"  # judges a line of her own: one short call with no tools, on her own share of the usage


class DeepThinkConfig(_Section):
    """Deep thinks (``tools/extensions/_deep_think.py``): a hard question handed to a brain of its own at a high
    effort while she keeps talking; she says the answer when it's ready. Voice mode only."""

    enabled: bool = True
    model: str = "opus"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # Questions she may take away to think about on her own a day; the ones you ask her to think hard about don't
    # count. Each costs about as much usage as a short background task.
    max_per_day: int = Field(8, ge=0, le=50)
    # Of those, second opinions she may ask for on her own a day (costly moments only); the ones you ask for don't count.
    max_second_opinions: int = Field(3, ge=0, le=20)
    time_limit_min: float = Field(10.0, gt=0, le=30)
    web: bool = True  # the thinking brain may search and read the web


class PlaybooksConfig(_Section):
    """Playbooks (``octavia/playbooks``, docs/playbooks.md): after each screen or web task of a few steps she rewrites
    short notes on the apps and sites it worked in (where things are, what worked, what failed and the fix), and the
    next task there starts with them. One small brain call per task, counted against her usage budget."""

    enabled: bool = True
    model: str = "sonnet"  # writes the notes; alias or full model id
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    min_steps: int = Field(3, ge=1, le=100)  # a task with fewer steps teaches nothing about the app


class SkillsConfig(_Section):
    """Skills (``octavia/skills``, docs/skills.md): step sequences her screen and web tasks worked out, or kept doing by
    hand, saved on the user's yes so a later task runs them in one call. Noticing repeats is counting, with no Claude
    usage."""

    enabled: bool = True
    notice_repeats: bool = True  # offer to save steps done by hand again and again
    min_tasks: int = Field(3, ge=2, le=50)  # tasks a run of steps must turn up in before it's offered
    min_steps: int = Field(4, ge=3, le=12)  # the shortest run worth a skill


class LoggingConfig(_Section):
    level: str = "INFO"
    file: str = "data/logs/octavia.log"
    console: Literal["quiet", "normal", "verbose"] = "normal"
    show_replies: bool = True


class TickTickConfig(_Section):
    """The user's TickTick (docs/ticktick.md). Connected by TICKTICK_API_TOKEN in .env; nothing here is secret."""

    # Completing, rescheduling, editing or moving tasks, checking in habits: allow | auto | confirm | deny.
    # auto = no question when you ask for it yourself; asked when she'd do it on her own.
    changes: Decision = Decision.AUTO
    deletes: Decision = Decision.CONFIRM  # deleting tasks, lists or habits
    reminders: bool = True  # say it out loud when a task with a time comes due
    remind_before_min: float = Field(0.0, ge=0, le=24 * 60)  # that many minutes before its time
    check_every_min: float = Field(5.0, ge=1, le=60)  # how often she looks for what's coming up (no Claude usage)
    morning_summary: str = "09:00"  # "HH:MM": say what's due today, once a day; "" = never

    @field_validator("morning_summary")
    @classmethod
    def _clock_time(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("ticktick.morning_summary must be a time like 09:00, or empty")
        return value


class SchedulerConfig(_Section):
    """Jobs she runs on her own: at set times, when the PC starts, when you're back, or when something needs you."""

    enabled: bool = True  # run the jobs (voice mode only); the tools that manage them work either way
    quiet_hours: str = "22:00-07:00"  # "HH:MM-HH:MM": jobs due then wait until it's over; "" = none
    # Creating or changing a job: allow | auto | confirm | deny. auto = no question when you ask for it yourself;
    # asked when she'd do it on her own. Deleting one is always asked, like other deletes.
    changes: Decision = Decision.AUTO
    check_every_s: float = Field(15.0, ge=1, le=300)  # how often she looks for jobs that are due (no Claude usage)

    @field_validator("quiet_hours")
    @classmethod
    def _clock_span(cls, value: str) -> str:
        value = value.replace(" ", "")
        clock = r"(?:[01]?\d|2[0-3]):[0-5]\d"
        if value and not re.fullmatch(f"{clock}-{clock}", value):
            raise ValueError("scheduler.quiet_hours must look like 22:00-07:00, or be empty")
        return value


FUTURES_SOURCES = ("assistant", "tools", "ticktick", "canvas", "gmail", "computer", "evolve", "timers", "calendar")
FUTURES_DOMAINS = (
    "assistant",
    "general",
    "computer",
    "productivity",
    "school",
    "communications",
    "time",
    "goals",
    "routines",
    "self",
    "relationships",
    "health",
    "finance",
    "employment",
    "people",
)


class FuturesConfig(_Section):
    """Octavia Futures: her evidence-based model of your world and of how she herself does (docs/futures.md). It all
    stays on this PC, in a database of its own, and none of it costs Claude usage."""

    enabled: bool = True
    database: str = "data/futures/futures.db"
    # Speaking up on her own: a heads-up about a to-do that tends to slip, an experiment's result. What you asked her
    # to watch for ("remind me when...") is always said; this switches off everything else.
    proactive: bool = True
    max_per_hour: int = Field(2, ge=0, le=20)  # at most this many remarks of her own an hour...
    max_per_day: int = Field(6, ge=0, le=100)  # ...and a day
    cooldown_h: float = Field(12.0, ge=0.5, le=24 * 14)  # the same remark about the same thing at most this often
    quiet_hours: str = "22:30-08:00"  # "HH:MM-HH:MM": nothing of her own then (it waits); "" = none
    heads_up_h: float = Field(3.0, ge=0.25, le=48)  # a heads-up about a to-do due within this many hours
    context_watch: bool = True  # notice which app is in front (names only, never window titles)
    canvas_watch: bool = True  # look at Canvas for new or changed assignments and grades (needs its token)
    # How often Canvas's planner is looked at (one small request, no Claude usage): an attendance check posted in
    # class is only open for an hour or two. Grades are looked at every few hours whatever this says.
    canvas_every_min: float = Field(15.0, ge=5, le=180)
    # Offering help before you ask (futures/offers.py): "That essay's due at 11. Want me to pull up the rubric?" An
    # offer only asks; anything risky the help needs is still asked as always. Off with `proactive` too.
    offers: bool = True
    offers_per_day: int = Field(3, ge=0, le=20)  # at most this many offers a day (within max_per_day)
    offer_assignment_h: float = Field(4.0, ge=0.5, le=48)  # offer help with Canvas work due within this many hours
    offer_meeting_min: float = Field(10.0, ge=1, le=120)  # offer an online meeting's link this long before it starts
    calendar_watch: bool = True  # read the calendar feeds every 15 minutes for online meetings (needs their address)
    # Apps you write in, by whole words of their name or program: bringing one to the front while Canvas work is
    # due within a day is the moment to offer its rubric.
    work_apps: list[str] = Field(
        default_factory=lambda: ["word", "winword", "notion", "obsidian", "onenote", "libreoffice", "soffice", "wps"]
    )
    keep_request_words: bool = True  # keep the words of your requests in episodes (anything secret is cut out)
    # What the sampled week assumes you can give to work (futures/simulate.py sample_week), drawn half either side
    week_free_hours_weekday: float = Field(3.0, ge=0, le=16)
    week_free_hours_weekend: float = Field(5.0, ge=0, le=16)
    retention_days: int = Field(180, ge=7, le=3650)  # raw events older than this are deleted; what was learned stays
    exclude_sources: list[str] = Field(default_factory=list)  # never recorded: gmail, canvas, ticktick, computer, ...
    off_limits: list[str] = Field(default_factory=list)  # domains never learned about or brought up: health, finance...
    experiments: Literal["off", "ask"] = "ask"  # off = never propose one; ask = propose, and run only with your yes

    @field_validator("quiet_hours")
    @classmethod
    def _clock_span(cls, value: str) -> str:
        value = value.replace(" ", "")
        clock = r"(?:[01]?\d|2[0-3]):[0-5]\d"
        if value and not re.fullmatch(f"{clock}-{clock}", value):
            raise ValueError("futures.quiet_hours must look like 22:30-08:00, or be empty")
        return value

    @field_validator("exclude_sources")
    @classmethod
    def _sources(cls, value: list[str]) -> list[str]:
        value = [v.strip().lower() for v in value if v.strip()]
        unknown = sorted(set(value) - set(FUTURES_SOURCES))
        if unknown:
            raise ValueError(f"futures.exclude_sources: unknown {unknown}; choose from {', '.join(FUTURES_SOURCES)}")
        return value

    @field_validator("off_limits")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        value = [v.strip().lower() for v in value if v.strip()]
        unknown = sorted(set(value) - set(FUTURES_DOMAINS))
        if unknown:
            raise ValueError(f"futures.off_limits: unknown {unknown}; choose from {', '.join(FUTURES_DOMAINS)}")
        return value


class ScreenHistoryConfig(_Section):
    """Screen history ("total recall", docs/screen-history.md): the text that was on screen, searchable, kept only on
    this PC. Never pictures, password fields, private windows, password managers or banking sites."""

    enabled: bool = True
    database: str = "data/recall/screen_history.db"
    poll_s: float = Field(2.0, ge=0.5, le=60)  # how often to check which window is in front (cheap)
    dwell_s: float = Field(3.0, ge=0, le=120)  # a window is read once it has been in front this long
    recapture_s: float = Field(30.0, ge=5, le=3600)  # and again this often while it stays there
    idle_after_s: float = Field(120.0, ge=10, le=3600)  # no key or mouse this long: stop looking
    max_cpu_percent: float = Field(85.0, ge=10, le=100)  # the PC busier than this: wait for the next look
    retention_days: float = Field(90.0, ge=1, le=3650)  # older text and windows are deleted
    max_mb: float = Field(1000.0, ge=10, le=100_000)  # the oldest days go once the database is bigger
    ocr: bool = True  # Windows' offline OCR for apps that show text UI Automation can't read
    ocr_every_s: float = Field(120.0, ge=10, le=3600)  # at most this often per window
    exclude_apps: list[str] = Field(default_factory=list)  # never kept, besides password managers (e.g. ["signal"])
    exclude_sites: list[str] = Field(default_factory=list)  # never kept, besides banks (e.g. ["mychart.org"])


class RoutingConfig(_Section):
    """How each spoken or typed turn is routed (docs/routing.md). Phase 1 of the multi-model plan: rules only."""

    # Quick commands ("set a timer for 5 minutes", "volume up", "next song", "what time is it") run their tool
    # directly, with no brain call: "on"; "shadow" = still use the brain, only log which rule would have fired; "off".
    fast_path: Literal["on", "shadow", "off"] = "on"
    # One line per turn (its route, latency, tools and usage) for `octavia routing`; it holds what you said.
    log: bool = True
    log_file: str = "data/routing_log.jsonl"


class FastChatConfig(_Section):
    """Her quick voice for small talk (``brain/fast_chat.py``, docs/routing.md): "how's it going", "that's
    hilarious", "I'm so tired today" are answered by a small, warm Claude session with no tools (Haiku), in about half
    a second instead of the brain's one to three. Anything it can't just chat about it hands to the brain, as before."""

    enabled: bool = True
    model: str = "haiku"  # alias or full model id; on your Claude subscription like the brain
    max_words: int = Field(16, ge=1)  # longer utterances go straight to the brain
    # The most it may take to start a real reply; slower (or a handoff, or an error) = the brain answers instead.
    timeout_s: float = Field(1.2, gt=0)
    context_turns: int = Field(6, ge=0)  # how many recent lines of the conversation it's shown, to stay on topic


class LocalBrainConfig(_Section):
    """A local model beside the brain, in shadow (``octavia.localbrain``, docs/local-brain.md): after each of your turns
    a small model on this PC's GPU (llama.cpp) is asked the same thing and only writes down what it would have done,
    its tool choice and how fast, next to what the brain did. It never answers, never runs a tool and never holds up
    a turn: it runs once the brain's turn is over. ``octavia shadow`` compares the two. While a game is in front it
    pauses, and after a while unloads the model so the game gets the GPU's memory back. ``mode`` lets it answer."""

    enabled: bool = True
    server: str = "E:/local-llm/llama.cpp/llama-server.exe"  # llama.cpp's Vulkan build; missing = off
    model: str = "E:/local-llm/models/Qwen3.5-4B-Q4_K_M.gguf"  # a GGUF; missing = off
    device: str = "Vulkan0"  # `llama-server --list-devices`
    port: int = Field(8091, ge=1024, le=65535)  # on 127.0.0.1 only
    context: int = Field(65536, ge=4096)  # her prompt and every tool's description is ~42k tokens
    history_turns: int = Field(3, ge=0, le=20)  # earlier exchanges it's shown, so a follow-up makes sense
    game_unload_s: float = Field(10.0, ge=0)  # a game in front this long: the model is unloaded (0 = never)
    game_reload_s: float = Field(120.0, ge=0)  # and loaded again once no game has been in front this long
    log_file: str = "data/local_shadow.jsonl"
    # Her read prompt is kept here between loads (~1.5 GB, one file): back in about a second instead of read again in
    # ~17 s after each game. "" = read it again every time.
    slot_cache_dir: str = "data/local_brain_cache"
    # "shadow": it only logs (above). "route": it hears your turns first and, in one reply, answers (with only
    # ``route_tools``) or hands the turn to the brain; risky or complex turns, and anything it gets wrong, are the
    # brain's; and it answers when Claude is down or out of usage. "fallback_only": only that last part
    # (``localbrain/router.py``, ``localbrain/delegation.py``).
    # These are set in config/local.yaml; ``exclude`` only because config/default.yaml doesn't list them yet (her own
    # builds can't change it): they're read and checked like any other setting.
    mode: Literal["shadow", "route", "fallback_only"] = Field("shadow", exclude=True)
    route_max_words: int = Field(12, ge=1, exclude=True)  # longer turns always go to the brain
    route_tools: list[str] = Field(
        default_factory=lambda: [
            "set_timer",
            "cancel_timer",
            "list_timers",
            "set_volume",
            "get_volume",
            "media_control",
        ],
        exclude=True,
    )
    # A routed turn's step slower than this: the brain answers instead.
    answer_timeout_s: float = Field(4.0, gt=0, exclude=True)
    # How sure the local model must be that a turn is its own (one minus its odds of handing it over, from its first
    # token's logprobs) to answer it; less sure, and the brain answers instead. 0.99: on 92 of the user's real turns
    # (2026-10-26) every turn the 9B kept rightly was at 1.0, and 18 of the 33 it kept wrongly were below 0.99.
    route_min_confidence: float = Field(0.99, ge=0, le=1, exclude=True)
    # The same while Claude is down, when nothing else can answer.
    fallback_timeout_s: float = Field(30.0, gt=0, exclude=True)
    # After Claude failed, your turns go to the local model first for this long.
    claude_retry_s: float = Field(300.0, ge=0, exclude=True)
    # One line per turn: which brain answered, and why.
    route_log_file: str = Field("data/local_route.jsonl", exclude=True)
    # Set to local only, she plays along with adult, crude or explicit talk instead of declining it in character
    # (the user's choice, 2026-10-26, with an uncensored model). Only the local model is told; Claude never is.
    uncensored: bool = Field(False, exclude=True)


class AddresseeConfig(_Section):
    """Was that for her? (``octavia.addressee``, docs/addressee.md): in shadow, the local model judges each thing you
    say near the PC without her name (what her second listen for the wake word already transcribes), from your words,
    how recently she spoke, what's in front, a call app on the mic and sound playing. It only writes down when she
    would have answered (with what you said) and counts the rest; she never answers on it. ``octavia addressee``
    shows how often she'd have jumped in. Needs the local brain (``local_brain``) and the second listen
    (``wake.second_chance``)."""

    enabled: bool = True
    log_file: str = "data/addressee.jsonl"
    # She'd have answered when the model is at least this sure it was for her (its own odds of "yes" over "no").
    threshold: float = Field(0.5, gt=0, lt=1)
    # Said again with her name within this long: it was for her (a miss when she wouldn't have answered).
    repeat_window_s: float = Field(30.0, ge=5, le=300)


class SelfScoringConfig(_Section):
    """Her own record of how she does (``futures.selfscore``, docs/futures.md): after every reply she judges how it
    went from what you said or did next (thanked, fine, or corrected, cut short, asked again...), with no model and no
    Claude usage, and runs small fair tests on herself (short answers when you sound stressed, leading with the answer,
    when a to-do heads-up comes). What a test clearly shows works better becomes a habit, which you can list and drop
    with a word ("drop that habit")."""

    enabled: bool = True
    # own: the tests that only change how she talks run on their own (anything that changes your day still asks you
    # first); ask: every test asks first; off: none.
    experiments: Literal["off", "ask", "own"] = "own"
    lessons: bool = True  # what she did in a situation and how it ended, back on her turn when it comes again
    # Written approaches to what keeps going wrong, tried on some turns and kept or dropped by how those go
    strategies: bool = True
    keep_habits: bool = True  # keep what a test shows works better (off: she only reports the result)


class EmotionConfig(_Section):
    """Hearing how the user sounds, not only what they say (``octavia.emotion``): a local speech-emotion model
    (emotion2vec+ base, ONNX on the CPU) plus their loudness, pitch and pace against their own usual. Everything stays
    on this PC and no audio is kept. It reads the utterance while the end-of-speech silence is still being waited out,
    so it's ready before the transcript; whatever isn't ready by then is skipped for that turn, never waited for."""

    enabled: bool = True
    # Where `octavia setup` puts the model (about 375 MB). Without it the feature is off and says so once at startup.
    model_dir: str = "models/emotion/emotion2vec-plus-base"
    # The most the brain waits for the tone once the transcript is ready; later than that it's skipped for the turn.
    max_wait_ms: int = Field(60, ge=0)
    # Only a tone at least this sure (and not "neutral") becomes a note for the brain.
    min_confidence: float = Field(0.55, ge=0, le=1)
    # Loudness, pitch and speaking rate compared with the user's running usual (data/emotion/baseline.json).
    prosody: bool = True
    note: bool = True  # tell the brain (a cautious note); off = only the ToneDetected event and the routing log
    num_threads: int = Field(4, ge=1)  # CPU threads for the model (Parakeet uses stt.num_threads beside it)
    # Only the last this-many seconds of a long utterance go through the model (the cost grows with the length).
    max_audio_s: float = Field(3.0, ge=1)


class HiveConfig(_Section):
    """The ETHER (src/octavia/hive/): signed updates from the maintainer, and fixes, lessons and test cases this
    install proposes back, scrubbed and only with the user's yes. Off unless turned on."""

    # Where updates are published: an https address or a folder holding channel.json and channel.json.sig ("" = none).
    feed: str = ""
    # The maintainer's public key (hex, Ed25519): an update whose signature doesn't check out is never applied.
    public_key: str = ""
    check_every_h: float = Field(24.0, ge=1, description="How often to look for an update")
    # off: nothing leaves this PC | on: fixes, lessons and failed cases go to the outbox, scrubbed, and are sent.
    share: Literal["off", "on"] = "off"
    # Where proposals go: an https address that takes a POST, or a folder (a synced one) ("" = kept in the outbox).
    inbox: str = ""


class DiscordBridgeConfig(_Section):
    """Texting her from your phone: DMs to a Discord bot of yours become turns, and she texts back
    (docs/discord-bridge.md). Voice mode only. The bot token lives in .env (DISCORD_BOT_TOKEN), never here."""

    enabled: bool = False
    # Your own numeric Discord user ID (Developer Mode, right-click yourself, Copy User ID); 0 = take it from .env's
    # DISCORD_OWNER_ID. Only this account is ever answered; without one the bridge doesn't start.
    owner_id: int = Field(0, ge=0)
    # Her announcements (a build ready, a timer, a reminder, a task done) also texted to you: "off", "away" (after
    # away_after_min without keyboard or mouse input on this PC) or "always".
    notify: Literal["off", "away", "always"] = "away"
    away_after_min: float = Field(10.0, ge=0)
    speak_replies: bool = False  # also say her texted replies out loud at the PC
    confirm_timeout_s: float = Field(120.0, gt=0)  # how long a "Want me to...? Reply yes or no." waits; then it's a no
    max_turns_per_10_min: int = Field(20, ge=1)  # beyond this many texted turns in 10 minutes she pauses
    max_file_mb: float = Field(25.0, gt=0)  # the biggest file she sends you (Discord's upload limit)
    max_attachment_mb: float = Field(25.0, gt=0)  # the biggest file of yours she saves; bigger ones are skipped
    # Calling her: when you join a voice channel in a server her bot is in, she joins too and the call is her ears and
    # voice until you leave. Only your voice is heard there.
    voice_calls: bool = True
    mute_pc_on_call: bool = True  # her voice goes to the call only, not out of this PC's speaker as well
    call_wake_word: bool = False  # on a call, everything you say is for her; true = say her name first, as at the PC


class CirclingConfig(_Section):
    """Circling something with the mouse, no buttons held, so it's what "this" means; docs/circling.md."""

    enabled: bool = True
    linger_s: float = Field(
        default=20.0, ge=3, le=300
    )  # how long after a circle she still takes it to be what you mean
    ring: bool = True  # a faint ring that fades in about a second, so you know she saw it


class AppConfig(_Section):
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    barge_in: BargeInConfig = Field(default_factory=BargeInConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    gods_eye: GodsEyeConfig = Field(default_factory=GodsEyeConfig)
    visuals: VisualsConfig = Field(default_factory=VisualsConfig)
    circling: CirclingConfig = Field(default_factory=CirclingConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    hotkeys: HotkeysConfig = Field(default_factory=HotkeysConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    self_improvement: SelfImprovementConfig = Field(default_factory=SelfImprovementConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    ticktick: TickTickConfig = Field(default_factory=TickTickConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    futures: FuturesConfig = Field(default_factory=FuturesConfig)
    screen_history: ScreenHistoryConfig = Field(default_factory=ScreenHistoryConfig)
    inner_life: InnerLifeConfig = Field(default_factory=InnerLifeConfig)
    routines: RoutinesConfig = Field(default_factory=RoutinesConfig)
    study: StudyConfig = Field(default_factory=StudyConfig)
    usage_budget: UsageBudgetConfig = Field(default_factory=UsageBudgetConfig)
    objectives: ObjectivesConfig = Field(default_factory=ObjectivesConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    deep_think: DeepThinkConfig = Field(default_factory=DeepThinkConfig)
    heads_up: HeadsUpConfig = Field(default_factory=HeadsUpConfig)
    finance: FinanceConfig = Field(default_factory=FinanceConfig)
    deadlines: DeadlinesConfig = Field(default_factory=DeadlinesConfig)
    unprompted: UnpromptedConfig = Field(default_factory=UnpromptedConfig)
    playbooks: PlaybooksConfig = Field(default_factory=PlaybooksConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    fast_chat: FastChatConfig = Field(default_factory=FastChatConfig)
    local_brain: LocalBrainConfig = Field(default_factory=LocalBrainConfig)
    claims: ClaimsConfig = Field(default_factory=ClaimsConfig)
    addressee: AddresseeConfig = Field(default_factory=AddresseeConfig)
    emotion: EmotionConfig = Field(default_factory=EmotionConfig)
    self_scoring: SelfScoringConfig = Field(default_factory=SelfScoringConfig)
    discord_bridge: DiscordBridgeConfig = Field(default_factory=DiscordBridgeConfig)
    hive: HiveConfig = Field(default_factory=HiveConfig)

    # Not part of the YAML schema; filled in by load_config.
    root: Path = Field(default_factory=find_project_root, exclude=True)
    sources: list[str] = Field(default_factory=list, exclude=True)
    # What she was started with on top of the files (``--verbose``, ``--wake``): kept for building it again.
    overrides: dict[str, Any] = Field(default_factory=dict, exclude=True)
    # Choices in data/settings.json left out because they don't fit (``build_config``); she says which.
    dropped_settings: list[str] = Field(default_factory=list, exclude=True)

    def resolve_path(self, path: str | Path) -> Path:
        """``path`` anchored at the project root.

        ``models`` and ``models/...`` go to OCTAVIA_MODELS_DIR if it is set and this config belongs to the
        checkout the package was imported from. A config on another root, such as a test's temporary folder,
        keeps ``<root>/models``: a test that downloads or deletes models there must not touch the shared ones.
        """
        p = Path(os.path.expandvars(str(path))).expanduser()
        if p.is_absolute():
            return p
        models = os.environ.get(MODELS_DIR_ENV, "").strip()
        if models and p.parts[:1] == (_MODELS,) and _is_checkout(self.root):
            base = Path(models).expanduser()
            return (base if base.is_absolute() else self.root / base).joinpath(*p.parts[1:])
        return self.root / p

    def claude_cli_path(self) -> str | None:
        """brain.claude.cli_path, with relative paths anchored at the project (autostart runs in System32).

        A bare command name ("claude") is left for PATH lookup.
        """
        value = self.brain.claude.cli_path
        if not value:
            return None
        if "/" in value or "\\" in value:
            return str(self.resolve_path(value))
        return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


# The .env file read at startup and the values it put into the environment, so ``current_env`` can follow later
# edits to the file without a restart.
_dotenv_file: Path | None = None
_dotenv_loaded: dict[str, str] = {}


def read_dotenv(path: Path) -> dict[str, str | None]:
    """The keys in the ``.env`` file at ``path`` ({} if it's missing), whatever encoding Windows saved it in.

    python-dotenv reads plain UTF-8, so Notepad's "UTF-8 with BOM" glued the byte-order mark to the first key
    (which then never matched its name) and a PowerShell ``Out-File`` (UTF-16) couldn't be read at all: either way
    a key the user had just added looked missing.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - python-dotenv is a declared dependency
        return {}
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return dict(dotenv_values(stream=io.StringIO(text)))


def dotenv_file() -> Path | None:
    """The ``.env`` file loaded at startup, the one ``current_env`` follows; None if none was loaded."""
    return _dotenv_file


def dotenv_supplied(name: str) -> bool:
    """Whether the variable ``name`` holds what ``.env`` gave it at startup, rather than a real environment
    variable's value (which wins over the file)."""
    return name in _dotenv_loaded and os.environ.get(name) == _dotenv_loaded[name]


def load_dotenv_file(root: Path) -> None:
    """Load ``.env`` from the project root without overriding real env vars."""
    global _dotenv_file, _dotenv_loaded
    from octavia.core.userdir import state_root

    env_file = state_root(root) / ".env"
    # Loading the same file again must not mistake what it set the first time for real environment variables.
    kept = {k: v for k, v in _dotenv_loaded.items() if os.environ.get(k) == v} if env_file == _dotenv_file else {}
    _dotenv_file, _dotenv_loaded = env_file, kept
    added = {k: v for k, v in read_dotenv(env_file).items() if v is not None and k not in os.environ}
    _dotenv_loaded |= added
    os.environ.update(added)


def current_env(name: str) -> str | None:
    """An environment variable, re-reading ``.env`` in case the user edited it since startup.

    The environment is loaded once at startup, so a key added to ``.env`` later would otherwise need a restart,
    which can kill work in progress. The file is only consulted while the variable still holds what ``.env`` gave it
    (or was unset in both): a real environment variable, or one changed at runtime, keeps winning as at startup,
    except an empty one (left behind by a launcher or an old session), which doesn't hide what the file says.
    """
    value = os.environ.get(name)
    from_file = value == _dotenv_loaded.get(name)
    if _dotenv_file is None or (not from_file and (value is None or value.strip())):
        return value
    try:
        if not _dotenv_file.exists():
            return value
        found = read_dotenv(_dotenv_file).get(name)
    except (OSError, ValueError):
        return value
    return found if from_file or found is not None else value


def _file_data(root: Path, path: str | Path | None) -> tuple[dict[str, Any], list[str]]:
    """``config/default.yaml`` with the local file merged over it, and the files read."""
    data: dict[str, Any] = {}
    sources: list[str] = []
    default_file = root / "config" / "default.yaml"
    if default_file.exists():
        data = _read_yaml(default_file)
        sources.append(str(default_file))
    public_file = root / "config" / "public.yaml"
    if edition.PUBLIC and public_file.exists():
        # The public build's defaults (src/octavia/edition.py): over default.yaml, under the user's own file.
        data = deep_merge(data, _read_yaml(public_file))
        sources.append(str(public_file))

    from octavia.core.userdir import state_root

    local = Path(path) if path else Path(os.environ.get("OCTAVIA_CONFIG", state_root(root) / "config" / "local.yaml"))
    if not local.is_absolute():
        local = root / local
    if local.exists():
        data = deep_merge(data, _read_yaml(local))
        sources.append(str(local))
    elif path or os.environ.get("OCTAVIA_CONFIG"):
        # An explicitly named file that's missing is a typo, not "use the defaults".
        raise FileNotFoundError(f"Config file not found: {local}")
    return data, sources


def file_values(root: Path | None = None, path: str | Path | None = None) -> dict[str, Any]:
    """What the config files alone give (no ``data/settings.json``, no overrides), unvalidated: ``path`` as in
    ``load_config``."""
    return _file_data((root or find_project_root()).resolve(), path)[0]


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    root: Path | None = None,
) -> AppConfig:
    root = (root or find_project_root()).resolve()
    load_dotenv_file(root)
    return build_config(path, overrides, root)


def build_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    root: Path | None = None,
) -> AppConfig:
    """``load_config`` without loading ``.env`` again: what the files say now, for picking up a change while she
    runs (``core/live_settings.py``)."""
    from octavia import user_settings

    root = (root or find_project_root()).resolve()
    data, sources = _file_data(root, path)
    chosen = user_settings.apply(copy.deepcopy(data), root)
    dropped: list[str] = []
    try:
        config = AppConfig.model_validate(deep_merge(chosen, overrides) if overrides else chosen)
    except ValueError as exc:
        if chosen == data:
            raise
        # A bad choice in data/settings.json must never keep her from starting, and one bad choice (a value caught
        # half typed) must not take the others with it: only the choices the errors point at are left out.
        dropped = _bad_choices(exc, user_settings.read(root))
        kept = user_settings.apply(copy.deepcopy(data), root, skip=set(dropped))
        try:
            config = AppConfig.model_validate(deep_merge(kept, overrides) if overrides else kept)
        except ValueError:
            log.warning("Ignoring %s: it doesn't fit the config", user_settings.FILE, exc_info=True)
            dropped = sorted(user_settings.read(root))
            config = AppConfig.model_validate(deep_merge(data, overrides) if overrides else data)
        else:
            log.warning(
                "Ignoring %s from %s: it doesn't fit the config (%s)", ", ".join(dropped), user_settings.FILE, exc
            )
    config.root = root
    config.sources = sources
    from octavia.core import home

    home.configure(config.assistant)
    config.overrides = dict(overrides or {})
    config.dropped_settings = dropped
    return config


def _bad_choices(exc: ValueError, saved: dict[str, Any]) -> list[str]:
    """The saved setting keys the validation errors point at: a key on an error's path, or one under it (all of
    them when the error doesn't say where)."""
    if not isinstance(exc, ValidationError):
        return sorted(saved)
    bad: set[str] = set()
    for error in exc.errors():
        path = ".".join(str(part) for part in error.get("loc", ()) if not isinstance(part, int))
        for key in saved:
            if not path or path == key or path.startswith(key + ".") or key.startswith(path + "."):
                bad.add(key)
    return sorted(bad)
