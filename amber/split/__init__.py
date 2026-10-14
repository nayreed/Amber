# amber/split/__init__.py
"""Run an Amber simulation split across a Reader host (gNB side) and Tag hosts (UE side).

Amber itself is used unchanged: the Reader host runs Amber's BSBehavior (Reader MAC and PHY),
each Tag host runs one Amber BackscatterModule with its controller and capacitor (Tag MAC and
PHY). The only cut is the direct call between them, replaced by proxies that turn it into
messages exchanged once per simulated millisecond.
"""
