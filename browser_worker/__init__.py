"""Safe, optional browser application worker.

The package does not import Playwright at module import time.  Production code may
install ``requirements-browser.txt`` and opt in to the Playwright runtime; tests
and other integrations can inject any runtime implementing the public protocols.
"""

from .evaluator import PageEvaluation, PageEvaluationClient, PageSnapshot
from .form_mapper import CandidateProfile, DeterministicFormMapper
from .worker import BrowserWorker, BrowserWorkerConfig, BrowserWorkerOutcome

__all__ = [
    "BrowserWorker",
    "BrowserWorkerConfig",
    "BrowserWorkerOutcome",
    "CandidateProfile",
    "DeterministicFormMapper",
    "PageEvaluation",
    "PageEvaluationClient",
    "PageSnapshot",
]
