"""Props she makes for the desktop avatar out of simple shapes, while she runs: "make yourself a sword", "hold the
flower again", "what props have you made?", "get rid of the sword".

A made prop is saved (``octavia.avatar.made_props``) and the app builds it at once (``AvatarBridge.define_prop``), no
restart; after one it's in the props the app starts with. She holds it, or any other prop she has, through
``AvatarBridge.hold_prop``: in her hand whenever an activity or moment doesn't need it for its own prop, until the
conversation about it is over, unless the user asked her to keep holding it (``keep``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import Field

from octavia.avatar.bridge import ORB_HOLDS_NO_PROPS, Prop, find_props, prop_name, scan_props
from octavia.avatar.made_props import MAX_MADE_PROPS, MadeProp, delete_made, read_made, save_made
from octavia.tools.base import NoArgs, RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

log = logging.getLogger(__name__)

NAME_MAX = 64  # the app's limit
NO_AVATAR = "The avatar isn't on (avatar.enabled in the config)."
KEEP = (
    "True only when the user asks her to keep holding it ('keep holding your sword', 'hold on to it'): then she "
    "holds it until told otherwise. Otherwise she puts it away by herself (still saved) once the conversation "
    "about it is over."
)


def _hold(avatar: Any, name: str | None, hand: str | None, keep: bool) -> str | None:
    """``hold_prop``, saying ``keep`` only when the user asked for it: a plain hold is what the avatar takes by
    default."""
    return avatar.hold_prop(name, hand, keep=True) if keep else avatar.hold_prop(name, hand)


def _avatar(ctx: ToolContext) -> Any | None:
    """The running avatar bridge, when it can take props; else None."""
    avatar = ctx.extras.get("avatar")
    if avatar is None or not callable(getattr(avatar, "define_prop", None)):
        return None
    return avatar


def _made_names(ctx: ToolContext) -> set[str]:
    made, _ = read_made(ctx.config)
    return set(made)


class MakeAvatarPropArgs(MadeProp):
    name: str = Field(
        min_length=1,
        max_length=NAME_MAX,
        description="A short name for it, e.g. 'sword'. The name of one made before replaces that one.",
    )
    hold: bool = Field(default=True, description="Hold it up right away (default); false only keeps it for later.")
    keep: bool = Field(default=False, description=KEEP)


class MakeAvatarProp(Tool):
    name = "make_avatar_prop"
    description = (
        "Make a new 3D prop for the desktop avatar (the character on the screen) out of simple shapes, and have her "
        "hold it, live: when the user asks her to make, build or hold something she has no prop for (a sword, a "
        "flower, a wand). It's saved, so it's still there after a restart. Build it at real size, in metres: the "
        "origin is the spot inside her fist, +y up and out of the top of her fist, +z its front. A part's position "
        "is its centre. Made again under the same name, it's replaced. hold_avatar_prop holds it (or another) later."
    )
    Args = MakeAvatarPropArgs
    risk = RiskLevel.SAFE  # her own cosmetic, undone by remove_avatar_prop; replacing one asks first
    category = "avatar"

    async def inspect_risk(self, args: MakeAvatarPropArgs, ctx: ToolContext) -> RiskFinding | None:
        if prop_name(args.name) in await asyncio.to_thread(_made_names, ctx):
            return RiskFinding(RiskLevel.CONFIRM, scope="replace", reason="throws away the one I made before")
        return None

    async def describe_call(self, args: MakeAvatarPropArgs, ctx: ToolContext) -> str:
        return f"make a new avatar prop called {prop_name(args.name)}"

    async def run(self, args: MakeAvatarPropArgs, ctx: ToolContext) -> ToolResult:
        name = prop_name(args.name)
        if not name:
            return ToolResult.error("That name needs a letter or a digit.")
        others, _ = await asyncio.to_thread(find_props, ctx.config.resolve_path(ctx.config.avatar.props_dir))
        if name in others:
            return ToolResult.error(f"There's already a prop called {name} (a file or a built-in): pick another name.")
        prop = MadeProp(parts=args.parts, hand=args.hand, grip=args.grip, rotation=args.rotation)
        try:
            saved = await asyncio.to_thread(save_made, ctx.config, name, prop)
        except OSError as exc:
            return ToolResult.error(f"Couldn't save the prop: {exc.strerror or exc}")
        if not saved:
            return ToolResult.error(f"She already has {MAX_MADE_PROPS} props she made: remove one first.")
        made = f"Made the prop {name} ({len(args.parts)} parts) and saved it"
        avatar = _avatar(ctx)
        if avatar is None:
            return ToolResult.success(f"{made}. {NO_AVATAR} She'll have it once it's on.")
        try:
            problem = avatar.define_prop(name, prop.to_prop(name).to_app())
            if problem is None and args.hold:
                problem = _hold(avatar, name, args.hand, args.keep)
        except Exception:
            log.warning("Giving the avatar its new prop failed", exc_info=True)
            return ToolResult.error(f"{made}, but the avatar didn't respond.")
        if problem == ORB_HOLDS_NO_PROPS:
            return ToolResult.success(f"{made}. {problem}")
        if problem:
            return ToolResult.success(f"{made}. {problem} She'll have it once the avatar is up.")
        if args.hold:
            return ToolResult.success(f"{made}; she's holding it in her {args.hand} hand{_until(args.keep)}.")
        return ToolResult.success(f"{made}; she isn't holding it.")


class HoldAvatarPropArgs(ToolArgs):
    name: str | None = Field(
        default=None,
        max_length=NAME_MAX,
        description="The prop to hold (list_avatar_props names them). Leave it out to put down the one she holds.",
    )
    hand: Literal["right", "left"] | None = Field(default=None, description="Only when the user says which hand.")
    keep: bool = Field(default=False, description=KEEP)


class HoldAvatarProp(Tool):
    name = "hold_avatar_prop"
    description = (
        "Have the desktop avatar hold one of her props (one she made, or a built-in like the mug or the umbrella), "
        "or put it down (no name): 'hold your sword again', 'put that down'. She puts it away by herself once the "
        "conversation about it is over or the next request is about something else, unless keep is true."
    )
    Args = HoldAvatarPropArgs
    risk = RiskLevel.SAFE
    category = "avatar"

    async def run(self, args: HoldAvatarPropArgs, ctx: ToolContext) -> ToolResult:
        avatar = _avatar(ctx)
        if avatar is None:
            return ToolResult.error(NO_AVATAR)
        name = prop_name(args.name) if args.name else None
        hand = args.hand
        if name is not None:
            props = (await asyncio.to_thread(scan_props, ctx.config)).props
            if name not in props:
                return ToolResult.error(f"There's no prop called {name}. Her props: {', '.join(sorted(props))}.")
            hand = hand or props[name].hand
        try:
            problem = _hold(avatar, name, args.hand, args.keep and name is not None)
        except Exception:
            log.warning("Asking the avatar to hold a prop failed", exc_info=True)
            return ToolResult.error("The avatar didn't respond.")
        if problem:
            return ToolResult.error(problem)
        if name is None:
            return ToolResult.success("She's put it down.")
        return ToolResult.success(f"She's holding the {name} in her {hand} hand{_until(args.keep)}.")


class ListAvatarProps(Tool):
    name = "list_avatar_props"
    description = (
        "List the desktop avatar's props: the ones she made with make_avatar_prop (and what they're made of), and "
        "the others she can hold."
    )
    Args = NoArgs
    risk = RiskLevel.SAFE
    category = "avatar"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        props = (await asyncio.to_thread(scan_props, ctx.config)).props
        made = {name: prop for name, prop in props.items() if prop.parts}
        others = sorted(name for name in props if name not in made)
        lines = [f"- {name}: {_made_of(prop)}, {prop.hand} hand" for name, prop in sorted(made.items())]
        text = "Props she made:\n" + "\n".join(lines) if lines else "She hasn't made any props yet."
        text += f"\nOther props: {', '.join(others)}."
        avatar = _avatar(ctx)
        held = getattr(avatar, "held_prop", None) if avatar is not None else None
        if isinstance(held, str):
            text += f"\nShe's holding the {held}."
        return ToolResult.success(text, data={"made": sorted(made), "others": others})


class RemoveAvatarPropArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=NAME_MAX, description="The name of the prop she made.")


class RemoveAvatarProp(Tool):
    name = "remove_avatar_prop"
    description = "Delete a prop the desktop avatar made with make_avatar_prop, for good (only those)."
    Args = RemoveAvatarPropArgs
    risk = RiskLevel.CONFIRM
    category = "avatar"

    async def describe_call(self, args: RemoveAvatarPropArgs, ctx: ToolContext) -> str:
        return f"delete the avatar prop {prop_name(args.name)} for good"

    async def precheck(self, args: RemoveAvatarPropArgs, ctx: ToolContext) -> str | None:
        name = prop_name(args.name)
        if name not in await asyncio.to_thread(_made_names, ctx):
            return f"She hasn't made a prop called {name or args.name}."
        return None

    async def run(self, args: RemoveAvatarPropArgs, ctx: ToolContext) -> ToolResult:
        name = prop_name(args.name)
        try:
            removed = await asyncio.to_thread(delete_made, ctx.config, name)
        except OSError as exc:
            return ToolResult.error(f"Couldn't delete it: {exc.strerror or exc}")
        if not removed:
            return ToolResult.error(f"She hasn't made a prop called {name or args.name}.")
        avatar = _avatar(ctx)
        if avatar is not None:
            try:
                avatar.remove_prop(name)
            except Exception:
                log.warning("Telling the avatar a prop is gone failed", exc_info=True)
        return ToolResult.success(f"Deleted the prop {name}.")


def _until(keep: bool) -> str:
    return " until told otherwise" if keep else ", and will put it away once the user moves on"


def _made_of(prop: Prop) -> str:
    """What a made prop is made of, in a few words: "2 cylinders, a box"."""
    counts: dict[str, int] = {}
    for part in prop.parts:
        counts[part["shape"]] = counts.get(part["shape"], 0) + 1
    return ", ".join(
        f"{n} {shape}{'es' if shape.endswith(('x', 's')) else 's'}" if n > 1 else f"a {shape}"
        for shape, n in counts.items()
    )


TOOLS = [MakeAvatarProp(), HoldAvatarProp(), ListAvatarProps(), RemoveAvatarProp()]
