"""Mail sync: fetch application mail from every configured account and run each new message
through the mail graph. `jobfit mail sync` (daily) and `jobfit mail backfill` (90 days) call it,
as does the dashboard's Sync mail button.

CONSTRAINT: logs carry counts per account label only. The daily run's GitHub Actions logs are
public, so no subjects, senders, companies or addresses.
CONSTRAINT: an account's sync cursor advances only when every message was processed, so a cost
cap stop, quota stop or provider error is retried next run (Message-ID dedup skips the rest).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from jobfit.applications.models import MailMessage, now
from jobfit.applications.store import AppStore
from jobfit.graphs.common import Deps
from jobfit.graphs.mail import build_mail_graph, run_mail
from jobfit.mail.config import AccountCfg, MailAuthExpired, MailConfig, secret

log = logging.getLogger("jobfit.mail.sync")


class Fetcher(Protocol):
    def fetch(self, cfg: MailConfig, since: datetime) -> Any: ...


@dataclass
class AccountReport:
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    created: int = 0
    matched: int = 0
    follow_up: int = 0
    review: int = 0
    event_only: int = 0
    failed: int = 0
    skipped: str | None = None  # "not configured"
    error: str | None = None


@dataclass
class MailReport:
    run_id: int
    status: str = "ok"  # ok | partial | capped | failed
    accounts: dict[str, AccountReport] = field(default_factory=dict)
    ghosted: int = 0
    spend_usd: float = 0.0
    stopped: str | None = None


def build_fetcher(label: str, acct: AccountCfg, cfg: MailConfig) -> Fetcher | None:
    """A fetcher for a configured account, or None when its secrets aren't set."""
    if acct.type == "gmail":
        from jobfit.mail.gmail import GmailFetcher

        cid, cs = secret(cfg.google.client_id_env), secret(cfg.google.client_secret_env)
        tok = secret(acct.refresh_token_env)
        if not (cid and cs and tok and acct.refresh_token_env):
            return None
        return GmailFetcher(label, cid, cs, tok, acct.refresh_token_env)
    from jobfit.mail.imap import ImapFetcher

    user, pw = secret(acct.user_env), secret(acct.password_env)
    if not (acct.host and user and pw and acct.password_env):
        return None
    return ImapFetcher(label, acct.host, user, pw, acct.folders, acct.password_env)


def run_mail_sync(
    deps: Deps,
    store: AppStore,
    cfg: MailConfig | None = None,
    *,
    days: int | None = None,
    fetchers: dict[str, Fetcher | None] | None = None,
    at: datetime | None = None,
) -> MailReport:
    cfg = cfg or MailConfig.load()
    at = at or now()
    rep = MailReport(store.start_run())
    spent_before = deps.llm.ledger.spent_today()
    graph = build_mail_graph(deps, store)
    state = store.sync_state()
    try:
        for label, acct in cfg.accounts.items():
            ar = rep.accounts.setdefault(label, AccountReport())
            if rep.stopped:
                ar.skipped = "stopped"
                continue
            fetcher = (
                fetchers.get(label) if fetchers is not None else build_fetcher(label, acct, cfg)
            )
            if fetcher is None:
                ar.skipped = "not configured"
                continue
            last = (state.get(label) or {}).get("last_synced_at")
            if days is not None:
                since = at - timedelta(days=days)
            elif last:
                since = last - timedelta(days=cfg.overlap_days)
            else:
                since = at - timedelta(days=cfg.lookback_days)
            try:
                msgs: list[MailMessage] = list(fetcher.fetch(cfg, since))
            except MailAuthExpired as e:
                ar.error = str(e)
                store.set_sync_state(label, synced_at=None, error=str(e))
                log.warning("%s: auth expired", label)
                continue
            except Exception as e:  # network/API: type only (messages could hold PII)
                ar.error = f"fetch failed: {type(e).__name__}"
                store.set_sync_state(label, synced_at=None, error=ar.error)
                log.warning("%s: %s", label, ar.error)
                continue
            ar.fetched = len(msgs)
            # Oldest first so status moves in the order things happened.
            for msg in sorted(msgs, key=lambda m: m.received_at):
                if store.has_message(msg.message_id):
                    ar.duplicates += 1
                    continue
                out = run_mail(msg, deps, store, graph)
                err = out.get("error")
                if err:
                    if err.startswith(("cost_cap", "quota")):
                        rep.stopped = err.split(":", 1)[0]
                        break
                    ar.failed += 1
                    continue
                ar.new += 1
                outcome = out.get("outcome", "")
                if hasattr(ar, outcome):
                    setattr(ar, outcome, getattr(ar, outcome) + 1)
            done = not rep.stopped and ar.failed == 0
            store.set_sync_state(label, synced_at=at if done else None, error=None)
            log.info("%s: %s", label, _counts(ar))
        rep.ghosted = store.mark_ghosted(at)
    except Exception:
        rep.status = "failed"
        raise
    finally:
        if rep.status != "failed":
            if rep.stopped:
                rep.status = "capped"
            elif any(a.error or a.failed for a in rep.accounts.values()):
                rep.status = "partial"
        try:
            rep.spend_usd = round(deps.llm.ledger.spent_today() - spent_before, 6)
        except Exception:
            log.exception("could not read spend")
        store.finish_run(rep.run_id, status=rep.status, stats=summary(rep), spend_usd=rep.spend_usd)
    return rep


def _counts(ar: AccountReport) -> dict[str, Any]:
    return {k: v for k, v in asdict(ar).items() if v and k != "error"}


def summary(rep: MailReport) -> dict[str, Any]:
    return {
        "status": rep.status,
        "stopped": rep.stopped,
        "ghosted": rep.ghosted,
        "spend_usd": rep.spend_usd,
        "accounts": {k: asdict(v) for k, v in rep.accounts.items()},
    }
