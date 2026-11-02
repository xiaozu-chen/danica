Fixed the failing assertion in cite_test.py: it now checks that the actual notice isn’t stored, rather than flagging any occurrence of “verified” in the conversation.

Patch note: Citation regression test now checks the emitted notice directly. All citation tests pass.