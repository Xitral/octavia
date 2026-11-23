"""The hivemind: every install gets better from what every other one learns, through the maintainer.

Nothing ever goes from one install straight into another. The flow is a star with a person at the middle:

* **Proposals go up** (``proposals.py``). With ``hive.share: on`` an install turns what it learned into proposals:
  a fix its self-improvement built and installed, a lesson (a playbook note, a habit), a request it got wrong
  (a test case). Each is scrubbed with the leak scanner first (``release/scan.py``, run with this install's own
  personal values), and one that still holds anything personal is kept back, never sent.
* **The maintainer reviews** (``review.py``). ``octavia hive review`` checks each proposal again, and ``accept`` puts a
  lesson or a case into the shared files in the repository and a fix onto a branch of its own, where the usual
  tests and the eval gate judge it. The shared test cases (``evolve/shared_cases.yaml``) are run on every build
  from then on, the owner's included: a fix has to work for everyone's failures, not only his.
* **Updates come down** (``channel.py``). A release is published to a feed (``channel.json``, signed with the
  maintainer's Ed25519 key) with its shared-eval pass rate. Installs check the signature with the public key they
  shipped with and apply only what checks out, keeping their own data, settings and keys.

What's measured: each release in the feed records how many shared cases it passes (``octavia hive status``), so
whether the collective is getting more capable is a number per release, not a feeling.
"""
