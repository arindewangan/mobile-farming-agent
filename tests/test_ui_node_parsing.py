"""
Parsing uiautomator's node tree.

THE BUG
`_NODE` matched only SELF-CLOSING tags: `<node … />`. uiautomator emits a leaf
that way, but a CONTAINER as `<node …>` with children — so every container node
was invisible to this module.

Measured on a real YouTube channel page: 114 node tags in the dump, 48 parsed,
and both video cells among the 66 that were dropped. A YouTube video row is an
android.view.ViewGroup holding the thumbnail and title as children, and it
carries its whole identity in its OWN content-desc:

    "<title> - <duration> - Go to channel - <channel> - <views> - <age> - play video"

So the rows were there, fully labelled, and unreachable. Downstream this looked
like "list still empty" while the screen plainly showed videos, `cell_probes`
(" views", " ago") never matching on a channel page, and picking falling
through to OCR and blind positional taps.

One regex; it repairs every flow that reads the tree, not just the new one.
"""
from __future__ import annotations

import recipeui

# The shape that was being dropped: a labelled ViewGroup with children.
CELL = (
    '<node index="1" text="" class="android.view.ViewGroup" '
    'content-desc="Jett + Shape Of You = PERFECTION | Valorant Edit - 36 seconds '
    '- Go to channel - Hype Gaming - 71 views - 1 day ago - play video" '
    'bounds="[0,1435][1080,1748]">'
    '<node index="0" text="" class="android.widget.ImageView" bounds="[0,1435][540,1748]" />'
    '<node index="1" text="" class="android.widget.TextView" bounds="[540,1435][1080,1748]" />'
    "</node>"
)
LEAF = '<node index="0" text="Subscribe" class="android.widget.Button" bounds="[48,990][530,1100]" />'
XML = f'<?xml version="1.0"?><hierarchy rotation="0">{CELL}{LEAF}</hierarchy>'


class TestContainersAreParsed:
    def test_a_container_node_is_not_skipped(self):
        """The whole bug in one assertion."""
        nodes = recipeui._parse_nodes(XML)
        assert any("play video" in (n.get("content-desc") or "") for n in nodes)

    def test_every_node_tag_is_parsed(self):
        assert len(recipeui._parse_nodes(XML)) == XML.count("<node")

    def test_leaves_still_parse(self):
        nodes = recipeui._parse_nodes(XML)
        assert any(n.get("text") == "Subscribe" for n in nodes)

    def test_a_container_keeps_its_own_bounds_not_a_child_s(self):
        """The tap centre must be the ROW's, not the thumbnail's — tapping a
        child can land on a sub-control instead of opening the video."""
        n = next(n for n in recipeui._parse_nodes(XML)
                 if "play video" in (n.get("content-desc") or ""))
        assert n["bounds"] == "[0,1435][1080,1748]"
        assert recipeui._center(n["bounds"]) == [540, 1591]


class TestTheProbesThatWereFailing:
    """These selectors already shipped and silently never matched on a channel
    page, because the text they look for lives on container nodes."""

    def test_cell_probes_match(self):
        nodes = recipeui._parse_nodes(XML)
        for probe in (" views", " ago"):
            assert recipeui._find_node(nodes, probe), probe

    def test_the_play_marker_matches(self):
        assert recipeui._find_node(recipeui._parse_nodes(XML), "play video")


class TestAttributeValuesContainingAngleBrackets:
    """`[^>]*` would end the tag at a `>` inside a quoted value. Video titles
    really do contain ">" ("BEFORE > AFTER"), and losing that node would look
    exactly like the original bug on precisely the rows most worth watching."""

    XML = ('<hierarchy><node class="x" content-desc="BEFORE &gt; AFTER" '
           'bounds="[0,0][10,10]"/>'
           '<node class="y" content-desc="A > B - play video" bounds="[0,20][10,30]"/>'
           "</hierarchy>")

    def test_a_bare_gt_inside_a_value_does_not_truncate_the_tag(self):
        nodes = recipeui._parse_nodes(self.XML)
        hit = [n for n in nodes if "play video" in (n.get("content-desc") or "")]
        assert len(hit) == 1
        assert hit[0]["bounds"] == "[0,20][10,30]"

    def test_both_nodes_are_still_found(self):
        assert len(recipeui._parse_nodes(self.XML)) == 2


class TestRealWorldShape:
    def test_a_nested_three_deep_tree_parses_every_level(self):
        xml = ('<hierarchy><node class="a" bounds="[0,0][1,1]">'
               '<node class="b" bounds="[0,0][1,1]">'
               '<node class="c" bounds="[0,0][1,1]" />'
               "</node></node></hierarchy>")
        assert {n["class"] for n in recipeui._parse_nodes(xml)} == {"a", "b", "c"}

    def test_an_empty_dump_yields_nothing_rather_than_raising(self):
        assert recipeui._parse_nodes("") == []
