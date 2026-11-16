import unittest

from src.execution.telemetry import rejection_quote


def event(identifier, kind, execution='a', symbol='MRVL', **detail):
    return dict(id=identifier, event=kind,
                detail=dict(execution_id=execution, symbol=symbol, **detail))


class RejectionQuoteTests(unittest.TestCase):
    def test_only_failed_attempt_and_best_of_its_last_two_directions(self):
        rejected = event(10, 'blocked')
        rows = [event(14, 'quote_evaluation', execution='b', symbol='BOT', net_edge_bps=99),
                rejected, event(9, 'stage_failed'),
                event(8, 'quote_evaluation', direction='sell_variational', quantity=1, net_edge_bps=-2.1),
                event(7, 'quote_evaluation', direction='buy_variational', quantity=1, net_edge_bps=-24),
                event(6, 'stage_completed'),
                event(5, 'quote_evaluation', direction='sell_variational', quantity=1, net_edge_bps=15)]
        self.assertEqual(rejection_quote(rows, rejected)['id'], 8)

    def test_does_not_mix_other_symbols_or_unknown_execution_ids(self):
        rejected = event(10, 'blocked')
        rows = [rejected, event(9, 'quote_evaluation', symbol='BOT', net_edge_bps=15),
                event(8, 'quote_evaluation', execution='b', net_edge_bps=12)]
        self.assertIsNone(rejection_quote(rows, rejected))
        self.assertIsNone(rejection_quote(rows, event(10, 'blocked', execution=None)))
        self.assertIsNone(rejection_quote(rows, None))

    def test_repeated_rounds_without_intermediate_stage_cannot_hide_latest_failure(self):
        rejected = event(10, 'blocked')
        rows = [rejected,
                event(9, 'quote_evaluation', direction='sell_variational', quantity=1, net_edge_bps=-2),
                event(8, 'quote_evaluation', direction='buy_variational', quantity=1, net_edge_bps=-3),
                event(7, 'quote_evaluation', direction='sell_variational', quantity=1, net_edge_bps=20)]
        self.assertEqual(rejection_quote(rows, rejected)['id'], 9)

    def test_matching_quote_missing_in_bounded_window_stays_unknown(self):
        rejected = event(10, 'blocked')
        self.assertIsNone(rejection_quote([rejected, event(9, 'stage_failed')], rejected))


if __name__ == '__main__':
    unittest.main()
