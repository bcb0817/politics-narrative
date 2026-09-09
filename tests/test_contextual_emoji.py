import os
import unittest
from unittest.mock import patch
import post
import topical_editor as editor
from tests.test_topical_editor import TEXT, SOURCE, item, draft


class ContextualEmojiTests(unittest.TestCase):
    def test_plain_text_remains_valid(self):
        self.assertEqual(editor.check_draft(draft(),SOURCE,item(),False),TEXT)

    def test_fixed_pin_opening_rejected(self):
        with self.assertRaisesRegex(ValueError,'fixed_pin_opening'):
            editor.check_draft(draft('📌 '+TEXT),SOURCE,item(),False)

    def test_relevant_emoji_not_globally_banned(self):
        text=TEXT+' 🚆'
        self.assertEqual(editor.check_draft(draft(text),SOURCE,item(),False),text)

    def test_legacy_env_cannot_require_emojis(self):
        with patch.dict(os.environ,{'EMOJI_REQUIRED':'true'}):
            reasons=post._candidate_quality_violations({'tweet_text':TEXT+'説明です。'*12},item())
        self.assertNotIn('emoji_required',reasons)

    def test_style_change_invalidates_draft_cache_key(self):
        cfg=editor.settings()
        old=dict(cfg);old.pop('emoji_style')
        self.assertNotEqual(editor.draft_key(item(),cfg),editor.draft_key(item(),old))

    def test_optional_worktree_repair_never_adds_pin(self):
        if not hasattr(post,'_repair_candidate_format'):
            return
        with patch.dict(os.environ,{'EMOJI_REQUIRED':'true'}):
            candidate=post._repair_candidate_format({'tweet_text':'📌 '+TEXT},item())
        self.assertFalse(candidate['tweet_text'].startswith('📌'))
