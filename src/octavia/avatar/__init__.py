"""The desktop avatar: a 3D character (VRM) on the desktop that shows what the assistant is doing.

The character is drawn by a separate Electron app (``avatar/`` at the project root). ``bridge.AvatarBridge`` starts
it, and turns the assistant's events into messages on the app's stdin (docs/avatar-protocol.md): its state, the
emotion of each sentence as it starts playing (``emotion``), and lip sync from the audio as it is played
(``lipsync``).
"""
