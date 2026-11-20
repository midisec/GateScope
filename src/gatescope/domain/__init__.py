"""Framework-independent domain types."""

from .modes import AuthorizationMode, Decision, Environment, EvidenceTier, ModeCombination
from .market import Bar, BarRevisionBook, Gap, GapReconciler
from .identity import Action, AuthorizationContext, Role

from gatescope.domain.paper_trading import (
    PaperBar,
    PaperCloseReason,
    PaperExactTerms,
    PaperExit,
    PaperPnl,
    PaperRiskPolicy,
    PaperSide,
    SignalTerms,
    apply_entry_slippage,
    entry_drift_bps,
    evaluate_paper_exit,
    paper_pnl,
    size_paper_terms,
)

__all__ = [
    "Action",
    "AuthorizationContext",
    "AuthorizationMode",
    "Bar",
    "BarRevisionBook",
    "Decision",
    "Environment",
    "EvidenceTier",
    "Gap",
    "GapReconciler",
    "ModeCombination",
    "PaperBar",
    "PaperCloseReason",
    "PaperExactTerms",
    "PaperExit",
    "PaperPnl",
    "PaperRiskPolicy",
    "PaperSide",
    "Role",
    "SignalTerms",
    "apply_entry_slippage",
    "entry_drift_bps",
    "evaluate_paper_exit",
    "paper_pnl",
    "size_paper_terms",
]
