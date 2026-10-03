"""Seven-day preregistered public-data execution shadow; NOT an ML trial.

Two frozen software controls, no selection/promotion/accounts. Each invocation
collects one UTC 4h slot; it does not wait, detach or run forever. Raw receipts,
attempts and registrations are retained; a failed slot is never retried.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from .binance_th import (BASE_URL, PUBLIC_PARAMS, BinanceTHPublicFeed, _decode,
                        capture_binance_th, parse_instruments, require_binance_config, verify_binance_capture)
from .domain import Mode, Quote, money, utc
from .dust_audit import verify_dust_ledger
from .engine import Coordinator
from .entry_policy import policies
from .importer import load_dataset
from .native_execution import NativePaperBroker, NativeRules, PROFILE
from .native_history import verify_native_history
from .report import read_report
from .storage import Store


PURPOSE = "Seven-day Binance TH public execution engineering shadow; NOT ML approval or profit validation"
CASES = ("cash", "baseline")
MAX_ATTEMPTS = 43  # Bootstrap + 6 UTC 4h slots/day for seven days.
FEE_POLICY = "adverse CEILING at current declared commission precision; UNVERIFIED account behavior"
REFERENCE_POLICY = "referencePrice is NOT a verified filter average; unknown averaging basis blocks fills"


class ShadowFeed(BinanceTHPublicFeed):
    allowed_params = {**PUBLIC_PARAMS, "referencePrice": {"symbol"}}


def canonical(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()


def code_hash():
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()


def write_json(path, value):
    with path.open("x") as stream:
        stream.write(json.dumps(value, default=str, sort_keys=True, indent=2) + "\n")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def register_shadow(history, cfg, output, *, now=None):
    require_binance_config(cfg)
    require(cfg.timeframe_minutes == 240 and cfg.warmup+2 <= 252, "Shadow requires native 4h warmup within its fixed 800-request budget")
    require(not output.exists(), "Shadow output exists; use a new directory")
    source = verify_native_history(history, cfg)
    at = utc(now or datetime.now(timezone.utc))
    require(utc(json.loads((history/"manifest.json").read_text())["end"]) <= at, "Shadow parent history contains future data")
    next_slot = datetime.fromtimestamp((int(at.timestamp())//cfg.seconds+1)*cfg.seconds, timezone.utc)
    plan = {"schema": 1, "purpose": PURPOSE, "registered_at": at.isoformat(),
            "signal_not_before": next_slot.isoformat(), "deadline": (at+timedelta(days=7)).isoformat(),
            "config": asdict(cfg), "config_hash": cfg.digest(), "code_hash": code_hash(),
            "history": str(history.resolve()), "history_manifest_sha256": hashlib.sha256((history/"manifest.json").read_bytes()).hexdigest(),
            "history_csv_sha256": source["csv_sha256"], "history_development_sha256": source["development_csv_sha256"],
            "history_scope": source["source_scope"], "historical_outcome_status": "Previously inspected development, never reused as new validation",
            "cases": list(CASES), "execution_profile": PROFILE, "fee_rounding": FEE_POLICY,
            "reference_interpretation": REFERENCE_POLICY,
            "history_bars_per_observation": cfg.warmup+2, "slot_seconds": cfg.seconds,
            "budget": {"days": 7, "attempts": MAX_ATTEMPTS, "requests": 800, "bytes": 32*1024*1024},
            "observation_criteria": {"minimum_forward_slots": 40, "complete_seven_days": True, "all_raw_and_ledger_audits": True},
            "bootstrap_policy": "First existing closed bar is observation/mark only; no strategy decisions until signal_not_before",
            "failure_policy": "Reserve slot before network; never retry a failed slot; incomplete/crashed portfolio updates require manual audit",
            "ml_study_status": "NOT STARTED; no model exists/locked; future ML requires a separate preregistration before approval prices",
            "models_trained": 0, "selection_performed": False, "approved_for_live": False, "default_policy_changed": False}
    output.mkdir(parents=True, exist_ok=False)
    (output/"attempts").mkdir()
    write_json(output/"registration.json", {"plan": plan, "sha256": hashlib.sha256(canonical(plan)).hexdigest()})
    write_json(output/"writer.lock", {})
    for case in CASES:
        with Store(output/(case+".sqlite"), cfg, Mode.PAPER, create=True, source=PURPOSE, execution_profile=PROFILE) as store:
            Coordinator(store, cfg, entry_policy=policies()[case])
            with store.transaction():
                store.set_meta("shadow_registration_sha256", hashlib.sha256(canonical(plan)).hexdigest())
                store.set_meta("fee_asset_policy", plan["fee_rounding"])
    write_json(output/"initial-ledgers.json", {case: verify_dust_ledger(output/(case+".sqlite"), cfg) for case in CASES})
    return {"status": "registered", "output": str(output), "signal_not_before": plan["signal_not_before"],
            "deadline": plan["deadline"], "models_trained": 0, "approved_for_live": False, "worker_running": False}


def load_plan(output, cfg, *, running=False):
    require_binance_config(cfg)
    envelope = json.loads((output/"registration.json").read_text())
    plan = envelope["plan"]
    require(set(envelope) == {"plan", "sha256"} and hashlib.sha256(canonical(plan)).hexdigest() == envelope["sha256"], "Shadow registration checksum mismatch")
    require(plan.get("purpose") == PURPOSE and plan.get("config_hash") == cfg.digest()
            and canonical(plan.get("config")) == canonical(asdict(cfg)) and plan.get("cases") == list(CASES)
            and plan.get("execution_profile") == PROFILE and plan.get("history_bars_per_observation") == cfg.warmup+2
            and plan.get("fee_rounding") == FEE_POLICY and plan.get("reference_interpretation") == REFERENCE_POLICY
            and plan.get("slot_seconds") == cfg.seconds and cfg.timeframe_minutes == 240 and cfg.warmup+2 <= 252
            and plan.get("budget") == {"days": 7, "attempts": MAX_ATTEMPTS, "requests": 800, "bytes": 32*1024*1024}
            and plan.get("observation_criteria") == {"minimum_forward_slots": 40, "complete_seven_days": True, "all_raw_and_ledger_audits": True}
            and utc(plan["deadline"]) == utc(plan["registered_at"])+timedelta(days=7)
            and utc(plan["signal_not_before"]) == datetime.fromtimestamp((int(utc(plan["registered_at"]).timestamp())//cfg.seconds+1)*cfg.seconds, timezone.utc),
            "Shadow fixed protocol/config mismatch")
    require(plan.get("models_trained") == 0 and all(plan.get(key) is False for key in ("selection_performed", "approved_for_live", "default_policy_changed")), "Shadow safety binding mismatch")
    if running:
        require(plan["code_hash"] == code_hash(), "Shadow package changed; freeze original runtime or register a new study")
    require(hashlib.sha256((Path(plan["history"])/"manifest.json").read_bytes()).hexdigest() == plan["history_manifest_sha256"], "Shadow parent source changed")
    source = verify_native_history(Path(plan["history"]), cfg)
    require(source["csv_sha256"] == plan["history_csv_sha256"] and source["development_csv_sha256"] == plan["history_development_sha256"]
            and source["source_scope"] == plan["history_scope"], "Shadow parent candle/scope binding changed")
    return plan, envelope["sha256"]


def attempts(output):
    return sorted((output/"attempts").iterdir())


def reference_record(payload, receipt, symbol, cfg):
    require(isinstance(payload, dict) and payload.get("symbol") == symbol.replace("/", ""), "Reference symbol mismatch")
    price, timestamp = money(payload["referencePrice"]), payload["timestamp"]
    require(price > 0 and isinstance(timestamp, int) and not isinstance(timestamp, bool), "Invalid reference price/time")
    event = datetime.fromtimestamp(timestamp/1000, timezone.utc)
    age = (utc(receipt["received_at"])-event).total_seconds()
    return {"symbol": symbol, "price": price, "event_at": event.isoformat(), "receipt_at": receipt["received_at"],
            "fresh_at_receipt": -5 <= age <= cfg.quote_max_age_seconds,
            "verified_filter_average": False, "window_minutes": None,
            "meaning": "Published reference only; averaging window/method not verified"}


def execution_probes(quotes, rules, instruments, cfg, at):
    result = {}
    for symbol, quote in quotes.items():
        rule, instrument = rules[symbol], instruments[symbol]
        quantity = instrument.round_quantity(Decimal(2)*instrument.min_notional/quote.ask)+instrument.quantity_step
        result[symbol] = {}
        for side in ("buy", "sell"):
            limit = rule.price(quote.ask if side == "buy" else quote.bid, side)
            result[symbol][side] = {"probe_only": True, "quantity": quantity, "limit": limit,
                                    "rejection": rule.reject(quantity, limit, side, at),
                                    "fee_upper_assumption": rule.fee(quantity, limit, cfg.costs.taker_fee, side)}
    return result


def observe_shadow(output, cfg, *, feed=None):
    plan, identity = load_plan(output, cfg, running=True)
    client = feed or ShadowFeed(plan["history_bars_per_observation"])
    require(isinstance(client, ShadowFeed) and client.history_bars == plan["history_bars_per_observation"], "Shadow requires bound public reader/history size")
    with (output/"writer.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Check earlier raw/accounting bindings BEFORE new network or mutation.
        # Already holding the writer lock; the public verifier would report busy.
        _verify_shadow(output, cfg)
        at = utc(client.clock())
        require(utc(plan["registered_at"]) <= at <= utc(plan["deadline"]), "Outside registered shadow period")
        previous = attempts(output)
        require(len(previous) < MAX_ATTEMPTS, "Shadow attempt budget exhausted")
        slot = int(at.timestamp())//cfg.seconds*cfg.seconds
        used_requests = used_bytes = 0
        for path in previous:
            reservation = json.loads((path/"reservation.json").read_text())
            require(reservation["slot"] != slot, "Shadow slot already reserved; no retries")
            require((path/"result.json").exists(), "Incomplete shadow attempt; audit before continuing")
            result = json.loads((path/"result.json").read_text())
            require(not (result["status"] == "failed" and result.get("portfolio_updated")), "Partial shadow portfolio update; manual audit required")
            used_requests += result["requests"]
            used_bytes += result["bytes"]
        require(used_requests+18 <= plan["budget"]["requests"] and used_bytes+client.max_bytes <= plan["budget"]["bytes"], "Shadow total request/byte budget exhausted")
        destination = output/"attempts"/f"{len(previous):04d}"
        destination.mkdir(exist_ok=False)
        write_json(destination/"reservation.json", {"at": at.isoformat(), "slot": slot, "registration_sha256": identity})
        error, updated, references, case_results = None, False, {}, {}
        client.reset_request_budget()
        try:
            manifest = capture_binance_th(cfg, destination/"market", history_bars=client.history_bars, feed=client)
            verify_binance_capture(destination/"market", cfg)
            quotes = {symbol: Quote(symbol, utc(row["time"]), money(row["bid"]), money(row["ask"])) for symbol, row in manifest["quotes"].items()}
            for symbol in sorted(quotes):
                payload = client.request("referencePrice", {"symbol": symbol.replace("/", "")})
                references[symbol] = reference_record(payload, client.requests[-1], symbol, cfg)
            now = utc(client.clock())
            require(now <= utc(plan["deadline"]) and all(0 <= (now-q.time).total_seconds() <= cfg.quote_max_age_seconds for q in quotes.values()), "Expired/stale shadow observation")
            require(code_hash() == plan["code_hash"], "Shadow source changed during observation")
            histories = load_dataset(destination/"market", cfg).bars
            require(all(int(rows[-1].end.timestamp()) == slot for rows in histories.values()), "Shadow crossed its reserved UTC slot")
            qualified = datetime.fromtimestamp(slot, timezone.utc) >= utc(plan["signal_not_before"])
            rules = {symbol: NativeRules(row) for symbol, row in manifest["native_market_metadata"].items()}
            instruments, _ = parse_instruments(_decode(client.requests[0]["raw"]), cfg)
            for case in CASES:
                with Store(output/(case+".sqlite"), cfg, Mode.PAPER, execution_profile=PROFILE) as store:
                    require(store.get_meta("shadow_registration_sha256") == identity, "Shadow ledger registration mismatch")
                    coordinator = Coordinator(store, cfg, instruments, entry_policy=policies()[case])
                    coordinator.broker = NativePaperBroker(cfg, rules, instruments)
                    coordinator.broker.observed_capacity = client.observed_capacity.copy()
                    updated = True
                    if qualified:
                        coordinator.cycle(now, histories, quotes)
                    else:
                        store.mark_nav(now, {symbol: quote.mid for symbol, quote in quotes.items()})
                case_results[case] = verify_dust_ledger(output/(case+".sqlite"), cfg)
            details = {"qualified_forward_slot": qualified, "observed_at": now.isoformat(), "references": references,
                       "execution_probes": execution_probes(quotes, rules, instruments, cfg, now),
                       "rules": {symbol: rule.payload() for symbol, rule in rules.items()}, "ledger_audits": case_results}
            write_json(destination/"observation.json", details)
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        raw_directory = destination/"receipts"
        raw_directory.mkdir()
        receipts = []
        for index, receipt in enumerate(client.requests):
            file = f"receipts/{index:04d}.json"
            with (destination/file).open("xb") as stream:
                stream.write(receipt["raw"])
            receipts.append({key: value for key, value in receipt.items() if key != "raw"} | {"file": file})
        write_json(destination/"receipts.json", {"receipts": receipts, "bytes": client.bytes_downloaded})
        result = {"status": "failed" if error else "observed", "error": error, "slot": slot, "requests": len(receipts),
                  "bytes": client.bytes_downloaded, "portfolio_updated": updated, "models_trained": 0,
                  "approved_for_live": False, "worker_running": False}
        write_json(destination/"result.json", result)
        return result


def verify_shadow(output, cfg):
    with (output/"writer.lock").open("rb") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "inconclusive", "read_only": True, "worker_running": True,
                    "error": "Shadow observation is writing; audit after completion", "models_trained": 0, "approved_for_live": False}
        return _verify_shadow(output, cfg)


def _verify_shadow(output, cfg):
    plan, identity = load_plan(output, cfg)
    paths, qualified, errors, reads, bytes_read = attempts(output), 0, [], 0, 0
    require(len(paths) <= MAX_ATTEMPTS, "Too many shadow attempts")
    slots = set()
    latest_audits = json.loads((output/"initial-ledgers.json").read_text())
    require(set(latest_audits) == set(CASES), "Shadow initial ledger case mismatch")
    execution_contexts, valuation_contexts, capacities = {}, {}, {}
    portfolio_unstable = False
    for index, destination in enumerate(paths):
        require(destination.name == f"{index:04d}" and not destination.is_symlink(), "Unexpected shadow attempt path")
        reservation = json.loads((destination/"reservation.json").read_text())
        at, slot = utc(reservation["at"]), reservation["slot"]
        require(reservation["registration_sha256"] == identity and slot == int(at.timestamp())//cfg.seconds*cfg.seconds
                and slot not in slots and utc(plan["registered_at"]) <= at <= utc(plan["deadline"]), "Shadow reservation/time mismatch")
        slots.add(slot)
        if not (destination/"result.json").exists():
            errors.append("incomplete attempt " + destination.name)
            portfolio_unstable = True
            continue
        result = json.loads((destination/"result.json").read_text())
        bundle = json.loads((destination/"receipts.json").read_text())
        receipts, count_bytes = bundle["receipts"], 0
        require(result["requests"] == len(receipts), "Shadow receipt count mismatch")
        for j, receipt in enumerate(receipts):
            path = destination/f"receipts/{j:04d}.json"
            require(receipt["file"] == f"receipts/{j:04d}.json" and not path.is_symlink(), "Unexpected shadow raw path")
            raw = path.read_bytes()
            count_bytes += len(raw)
            require(hashlib.sha256(raw).hexdigest() == receipt["sha256"], "Shadow raw checksum mismatch")
            endpoint, params = receipt["endpoint"], receipt["params"]
            require(endpoint in ShadowFeed.allowed_params and not params.keys()-ShadowFeed.allowed_params[endpoint], "Shadow private/unallowlisted receipt")
            expected_url = urlsplit(BASE_URL+"/api/v1/"+endpoint+("?"+urlencode(params) if params else ""))
            actual_url = urlsplit(receipt["url"])
            require((actual_url.scheme, actual_url.netloc, actual_url.path) == (expected_url.scheme, expected_url.netloc, expected_url.path)
                    and not actual_url.fragment and parse_qs(actual_url.query) == parse_qs(expected_url.query), "Shadow public URL mismatch")
            requested, received = utc(receipt["requested_at"]), utc(receipt["received_at"])
            require(at <= requested <= received and (received-requested).total_seconds() <= 5, "Shadow receipt chronology/latency mismatch")
            if j:
                require(utc(receipts[j-1]["received_at"]) <= requested, "Shadow receipt ordering mismatch")
        require(count_bytes == bundle["bytes"] == result["bytes"], "Shadow byte count mismatch")
        reads += len(receipts)
        bytes_read += count_bytes
        if result["status"] != "observed":
            errors.append(result["error"] or "failed observation")
            portfolio_unstable |= bool(result.get("portfolio_updated"))
            continue
        market = json.loads((destination/"market/manifest.json").read_text())
        verify_binance_capture(destination/"market", cfg)
        require(len(receipts) == len(market["receipts"])+2, "Shadow reference receipt count mismatch")
        require(all(n == plan["history_bars_per_observation"] for n in market["bars_per_symbol"].values()), "Shadow registered warmup changed")
        for left, right in zip(receipts, market["receipts"]):
            require({k:v for k,v in left.items() if k != "file"} == {k:v for k,v in right.items() if k != "file"}, "Shadow market receipt projection mismatch")
        details = json.loads((destination/"observation.json").read_text())
        require(at <= utc(details["observed_at"]) <= utc(plan["deadline"]), "Shadow observation time mismatch")
        require((utc(details["observed_at"])-utc(receipts[0]["requested_at"])).total_seconds() <= 120,
                "Shadow per-observation elapsed budget mismatch")
        refs = {}
        for j, symbol in enumerate(sorted(i.symbol for i in cfg.instruments)):
            receipt = receipts[-2+j]
            require(receipt["endpoint"] == "referencePrice" and receipt["params"] == {"symbol":symbol.replace("/", "")}
                    and receipt["url"] == BASE_URL+"/api/v1/referencePrice?symbol="+symbol.replace("/", ""), "Shadow reference public URL/identity mismatch")
            refs[symbol] = reference_record(_decode((destination/receipt["file"]).read_bytes()), receipt, symbol, cfg)
        require(json.loads(json.dumps(refs, default=str)) == details["references"], "Shadow reference projection mismatch")
        expected_qualified = datetime.fromtimestamp(slot, timezone.utc) >= utc(plan["signal_not_before"])
        require(details["qualified_forward_slot"] is expected_qualified and utc(market["end_exclusive"]).timestamp() == slot, "Shadow bootstrap/forward boundary mismatch")
        qualified += int(expected_qualified)
        rules = {symbol: NativeRules(row) for symbol, row in market["native_market_metadata"].items()}
        require({symbol:rule.payload() for symbol, rule in rules.items()} == details["rules"], "Shadow execution rule projection mismatch")
        quotes = {}
        for symbol, row in market["quotes"].items():
            quote = Quote(symbol, utc(row["time"]), money(row["bid"]), money(row["ask"]))
            quotes[symbol] = quote
            require(0 <= (utc(details["observed_at"])-quote.time).total_seconds() <= cfg.quote_max_age_seconds, "Shadow stale/future quote")
            execution_contexts[(symbol, quote.time.isoformat())] = (rules[symbol], quote)
        valuation_contexts[utc(details["observed_at"]).isoformat()] = {s: str(q.mid) for s, q in quotes.items()}
        for receipt in market["receipts"]:
            if receipt["endpoint"] == "ticker/bookTicker":
                book = _decode((destination/"market"/receipt["file"]).read_bytes())
                symbol = next(s for s in quotes if s.replace("/", "") == book["symbol"])
                for side, field in (("buy", "askQty"), ("sell", "bidQty")):
                    capacities[(symbol, quotes[symbol].time.isoformat(), side)] = money(book[field])
        instruments, _ = parse_instruments({"symbols":list(market["native_market_metadata"].values())}, cfg)
        require(canonical(execution_probes(quotes,rules,instruments,cfg,utc(details["observed_at"]))) == canonical(details["execution_probes"]),
                "Shadow execution probe/fee projection mismatch")
        latest_audits = details["ledger_audits"]
    require(reads <= plan["budget"]["requests"] and bytes_read <= plan["budget"]["bytes"], "Shadow aggregate budget mismatch")
    reports, audits = {}, {}
    for case in CASES:
        path = output/(case+".sqlite")
        with sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            meta = dict(db.execute("SELECT key,value FROM metadata"))
            require(meta.get("execution_profile") == PROFILE and meta.get("shadow_registration_sha256") == identity
                    and json.loads(meta["entry_policy"])["name"] == case, "Shadow ledger case/profile binding mismatch")
            consumed = {}
            for fill in db.execute("SELECT f.*,o.symbol,o.side,o.quantity AS requested,o.price_limit FROM fills f JOIN orders o ON o.id=f.order_id"):
                require((fill["symbol"], fill["at"]) in execution_contexts, "Shadow fill has no fresh public observation")
                rule, quote = execution_contexts[(fill["symbol"], fill["at"])]
                require(not rule.reject(money(fill["requested"]), money(fill["price_limit"]), fill["side"], quote.time), "Shadow fill used rejected/unverified filters")
                buy = fill["side"] == "buy"
                adverse = quote.ask*(Decimal(1)+cfg.costs.slippage) if buy else quote.bid*(Decimal(1)-cfg.costs.slippage)
                price_filter = rule.filters["PRICE_FILTER"]
                offset, tick = money(price_filter["minPrice"]), money(price_filter["tickSize"])
                expected_price = offset+((adverse-offset)/tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)*tick if tick else adverse
                require(money(fill["price"]) == expected_price, "Shadow fill adverse price/tick mismatch")
                limit = money(fill["price_limit"])
                require(expected_price <= limit if buy else expected_price >= limit, "Shadow fill crossed its limit")
                capacity_key = (fill["symbol"], fill["at"], fill["side"])
                consumed[capacity_key] = consumed.get(capacity_key, Decimal(0))+money(fill["quantity"])
                require(consumed[capacity_key] <= capacities[capacity_key], "Shadow reused/exceeded public top-of-book capacity")
                basis = money(fill["quantity"]) * (Decimal(1) if fill["side"] == "buy" else money(fill["price"]))
                digits = rule.base_precision if fill["side"] == "buy" else rule.quote_precision
                expected_fee = (basis*cfg.costs.taker_fee).quantize(Decimal(10)**-digits, rounding=ROUND_CEILING)
                require(money(fill["fee"]) == expected_fee, "Shadow fill commission rounding mismatch")
            if not portfolio_unstable:
                for valuation in db.execute("SELECT at,marks FROM valuation_snapshots"):
                    require(valuation["at"] in valuation_contexts and json.loads(valuation["marks"]) == valuation_contexts[valuation["at"]],
                            "Shadow NAV mark has no matching public observation")
        audits[case] = verify_dust_ledger(path, cfg)
        if not portfolio_unstable:
            require(audits[case]["content_sha256"] == latest_audits[case]["content_sha256"], "Shadow ledger changed outside its observation")
        reports[case] = read_report(path)
    return {"status": "verified" if not errors else "inconclusive", "read_only": True, "scope": PURPOSE,
            "attempts": len(paths), "qualified_forward_slots": qualified, "minimum_forward_slots": 40,
            "requests_verified": reads, "bytes_verified": bytes_read, "errors": errors, "cases": reports, "ledger_audits": audits,
            "engineering_observations_complete": not errors and qualified >= 40 and datetime.now(timezone.utc) >= utc(plan["deadline"]),
            "execution_evidence": "INCONCLUSIVE: filter averaging basis and actual fee rounding unverified",
            "profit_evidence": "INCONCLUSIVE: seven-day engineering controls are NOT ML/profit approval",
            "models_trained": 0, "selection_performed": False, "approved_for_live": False,
            "current_package_matches_registration": code_hash() == plan["code_hash"], "worker_running": False}
