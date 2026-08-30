"""longest_path / find_cycle in linear-crud/scripts/board.py.

The graph helpers are the only part of the board that is not a Linear call,
and the critical-path note is built on them. Run by check-linear-crud.sh with
SKILLS_DIR pointing at a scratch copy of dot_claude/skills.
"""

import unittest

from _load import load_module

board = load_module("board", "linear-crud", "executable_board.py")


class LongestPath(unittest.TestCase):
    def test_empty_graph_has_no_path(self):
        self.assertEqual(board.longest_path({}), [])

    def test_chain_is_returned_in_order(self):
        g = {"A": ["B"], "B": ["C"], "C": ["D"]}
        self.assertEqual(board.longest_path(g), ["A", "B", "C", "D"])

    def test_diamond_picks_one_full_length_route(self):
        g = {"A": ["B", "C"], "B": ["D"], "C": ["D"]}
        path = board.longest_path(g)
        self.assertEqual(len(path), 3)
        self.assertEqual(path[0], "A")
        self.assertEqual(path[-1], "D")
        self.assertIn(path[1], ("B", "C"))

    def test_longest_branch_wins_over_a_short_one_listed_first(self):
        g = {"A": ["B", "C"], "C": ["D"], "D": ["E"]}
        self.assertEqual(board.longest_path(g), ["A", "C", "D", "E"])

    # NEGATIVE: a single edge must not be reported as anything longer.
    def test_single_edge_is_two_deep_not_more(self):
        self.assertEqual(board.longest_path({"A": ["B"]}), ["A", "B"])
        self.assertNotEqual(len(board.longest_path({"A": ["B"]})), 3)


class FindCycle(unittest.TestCase):
    def test_empty_graph_has_no_cycle(self):
        self.assertIsNone(board.find_cycle({}))

    # NEGATIVE for the cycle detector: a DAG (chain and diamond) is clean.
    def test_chain_and_diamond_have_no_cycle(self):
        self.assertIsNone(board.find_cycle({"A": ["B"], "B": ["C"]}))
        self.assertIsNone(board.find_cycle({"A": ["B", "C"], "B": ["D"], "C": ["D"]}))

    def test_two_node_cycle_is_found_and_closed(self):
        cyc = board.find_cycle({"A": ["B"], "B": ["A"]})
        self.assertIsNotNone(cyc)
        self.assertEqual(cyc[0], cyc[-1])
        self.assertEqual(set(cyc), {"A", "B"})

    def test_cycle_reached_through_a_tail_is_found(self):
        cyc = board.find_cycle({"X": ["A"], "A": ["B"], "B": ["C"], "C": ["A"]})
        self.assertIsNotNone(cyc)
        self.assertEqual(cyc[0], cyc[-1])
        self.assertNotIn("X", cyc)

    def test_self_loop_is_a_cycle(self):
        self.assertEqual(board.find_cycle({"A": ["A"]}), ["A", "A"])


if __name__ == "__main__":
    unittest.main()
