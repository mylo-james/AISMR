"""Property-based tests for workflow parsers using Hypothesis.

These tests ensure parsers handle edge cases gracefully without crashing.
"""

import json

from hypothesis import given, strategies as st, settings, assume

from myloware.workflows.parsers import extract_topic_from_brief, parse_structured_ideation

# Strategy for valid ideation JSON content
# Use alphanumeric + space characters to avoid JSON-breaking chars like }, ], "
safe_text = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(
        whitelist_categories=("L", "N", "Zs"), min_codepoint=32, max_codepoint=126
    ),
).filter(lambda x: x.strip())

valid_idea = st.fixed_dictionaries(
    {
        "title": safe_text,
        "visual_prompt": safe_text,
        "voice_over": safe_text,
    }
)

valid_ideation_json = st.fixed_dictionaries(
    {
        "ideas": st.lists(valid_idea, min_size=1, max_size=12),
    }
)


class TestParseStructuredIdeation:
    """Property-based tests for parse_structured_ideation."""

    @given(st.text())
    @settings(max_examples=100)
    def test_never_raises_on_arbitrary_input(self, text: str):
        """Parser should never crash on arbitrary input."""
        # Should return None or valid dict, never raise
        result = parse_structured_ideation(text)
        assert result is None or isinstance(result, dict)

    @given(valid_ideation_json)
    @settings(max_examples=50)
    def test_parses_valid_fenced_json(self, data: dict):
        """Parser should extract valid JSON from fenced code blocks."""
        # Create a fenced JSON block
        json_str = json.dumps(data)
        text = f"Here are some ideas:\n\n```json\n{json_str}\n```"

        result = parse_structured_ideation(text)
        assert result is not None
        assert "ideas" in result
        assert len(result["ideas"]) == len(data["ideas"])

    @given(valid_ideation_json)
    @settings(max_examples=50)
    def test_parses_bare_json(self, data: dict):
        """Parser should extract bare JSON with ideas array."""
        # Create text with bare JSON
        json_str = json.dumps(data)
        text = f"Based on your brief, here's what I suggest:\n\n{json_str}"

        result = parse_structured_ideation(text)
        assert result is not None
        assert "ideas" in result

    @given(st.text())
    @settings(max_examples=50)
    def test_returns_none_for_text_without_json(self, text: str):
        """Parser should return None when no valid JSON is present."""
        # Assume text doesn't contain JSON-like structures
        assume("{" not in text and "ideas" not in text.lower())

        result = parse_structured_ideation(text)
        assert result is None

    @given(st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=20))
    @settings(max_examples=50)
    def test_handles_malformed_json_gracefully(self, strings: list):
        """Parser should handle malformed JSON without crashing."""
        # Create something that looks like JSON but isn't valid
        fake_json = "{ " + ", ".join(f'"{s}"' for s in strings) + " }"
        text = f"```json\n{fake_json}\n```"

        # Should not raise, just return None
        result = parse_structured_ideation(text)
        assert result is None or isinstance(result, dict)


class TestExtractTopicFromBrief:
    """Property-based tests for extract_topic_from_brief."""

    @given(st.text())
    @settings(max_examples=100)
    def test_never_raises_on_arbitrary_input(self, brief: str):
        """Topic extraction should never crash on arbitrary input."""
        result = extract_topic_from_brief(brief)
        assert isinstance(result, str)

    @given(st.text(min_size=1, max_size=50).filter(lambda x: x.strip()))
    @settings(max_examples=50)
    def test_extracts_topic_from_about_pattern(self, topic: str):
        """Should extract topic after 'about' keyword."""
        assume(not any(c in topic for c in ["\n", "\r"]))

        brief = f"Create an ASMR video about {topic}"
        result = extract_topic_from_brief(brief)

        # Result should contain the topic (case-insensitive)
        assert topic.lower().strip().rstrip(".,!?") in result or result in topic.lower()

    @given(
        st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cc", "Cs")))
    )
    @settings(max_examples=50)
    def test_extracts_topic_from_run_aismr_pattern(self, topic: str):
        """Should extract topic from 'run aismr about X' pattern."""
        assume(topic.strip())
        assume(not any(c in topic for c in ["\n", "\r", "about", "featuring"]))
        # Skip topics that become empty after rstrip
        expected = topic.lower().strip().rstrip(".,!?")
        assume(expected)

        brief = f"run aismr about {topic}"
        result = extract_topic_from_brief(brief)

        # Result should be the topic (lowercase, stripped)
        assert result == expected

    @given(
        st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(
                whitelist_categories=("L", "N"), min_codepoint=32, max_codepoint=126
            ),
        )
    )
    @settings(max_examples=50)
    def test_single_word_returns_itself(self, word: str):
        """Single word brief should return itself (lowercase)."""
        assume(word.strip())
        assume(" " not in word)

        result = extract_topic_from_brief(word)
        expected = word.lower().strip().rstrip(".,!?")
        assume(expected)  # Skip empty results
        assert result == expected

    @given(st.text())
    @settings(max_examples=50)
    def test_result_is_lowercase(self, brief: str):
        """Result should always be lowercase."""
        result = extract_topic_from_brief(brief)
        assert result == result.lower()

    @given(st.text(min_size=0, max_size=100))
    @settings(max_examples=50)
    def test_result_has_no_trailing_punctuation(self, brief: str):
        """Result should not have trailing punctuation."""
        assume(brief.strip())  # Skip empty briefs

        result = extract_topic_from_brief(brief)
        # If result is not empty, it shouldn't end with these
        if result:
            assert not result.endswith((".", ",", "!", "?"))
