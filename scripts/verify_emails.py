"""verify_emails.py

Perform MX verification and generic address checks on candidate emails.

Input: contacts_raw.csv with column `email` and `company`.
Output: contacts_verified.csv with added columns: mx_pass (TRUE/FALSE), reject_reason, verification_score
"""
import csv
import argparse
import socket
import dns.resolver
import whois
import smtplib
import random
import string
import time
import requests
from datetime import datetime, UTC

GENERIC_LOCAL = ['info', 'hello', 'contact', 'admin', 'support', 'office', 'team', 'sales', 'noreply', 'mail']

# SMTP probe ports to try in order: standard (25) then submission (587).
# Port 465 requires smtplib.SMTP_SSL (different code path) so is omitted here.
# Port 587 rarely allows unauthenticated RCPT probes but is worth trying when
# port 25 is blocked at the egress firewall (common in cloud dev environments).
SMTP_PROBE_PORTS = [25, 587]

# Well-known hosted-mail providers — MX hostname suffix → provider name.
# Used for proxy-signal scoring when direct SMTP probing is transport-blocked.
KNOWN_MAIL_PROVIDERS = {
    'aspmx.l.google.com': 'google_workspace',
    'alt1.aspmx.l.google.com': 'google_workspace',
    'alt2.aspmx.l.google.com': 'google_workspace',
    'googlemail.com': 'google_workspace',
    'mail.protection.outlook.com': 'microsoft_365',
    'smtp.secureserver.net': 'godaddy',
    'mailstore1.secureserver.net': 'godaddy',
    'mx.zoho.com': 'zoho',
    'mx2.zoho.com': 'zoho',
    'in1-smtp.messagingengine.com': 'fastmail',
    'in2-smtp.messagingengine.com': 'fastmail',
    'mx1.privateemail.com': 'namecheap',
    'mx2.privateemail.com': 'namecheap',
}


def detect_mx_provider(mx_host: str) -> str:
    """Return the mail hosting provider name for an MX hostname, or empty string."""
    h = mx_host.lower().rstrip('.')
    if h in KNOWN_MAIL_PROVIDERS:
        return KNOWN_MAIL_PROVIDERS[h]
    for known, provider in KNOWN_MAIL_PROVIDERS.items():
        if h.endswith('.' + known):
            return provider
    return ''


def coerce_bool(value):
    """Normalize common truthy / falsy API values to bool or None."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {'true', '1', 'yes', 'y', 'ok', 'pass', 'deliverable', 'valid', 'accept', 'accepted'}:
        return True
    if text in {'false', '0', 'no', 'n', 'fail', 'undeliverable', 'invalid', 'reject', 'rejected'}:
        return False
    return None


def lookup_payload_value(payload, keys):
    """Search for the first matching key in a nested API payload."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload:
            return payload[key]
    for value in payload.values():
        if isinstance(value, dict):
            found = lookup_payload_value(value, keys)
            if found is not None:
                return found
    return None


def parse_verifier_api_result(payload):
    """Map common self-hosted verifier JSON payloads into internal verdict fields."""
    data = payload or {}
    if isinstance(data, dict):
        for wrapper_key in ('data', 'result', 'response'):
            wrapped = data.get(wrapper_key)
            if isinstance(wrapped, dict):
                data = wrapped
                break

    deliverable = coerce_bool(lookup_payload_value(data, (
        'deliverable', 'is_deliverable', 'is_reachable', 'reachable', 'valid', 'smtp_check'
    )))
    catch_all = coerce_bool(lookup_payload_value(data, (
        'catch_all', 'catchall', 'accept_all', 'acceptAll', 'is_catchall'
    )))
    mx_found = coerce_bool(lookup_payload_value(data, (
        'mx_found', 'mxFound', 'mx_lookup', 'has_mx_records', 'mx', 'mx_records'
    )))
    raw_status = lookup_payload_value(data, ('status', 'result', 'reason', 'message', 'smtp_status'))

    if deliverable is True:
        smtp_ok = 'ACCEPT'
    elif deliverable is False and (mx_found is not False or raw_status is not None):
        smtp_ok = 'REJECT'
    else:
        smtp_ok = 'UNKNOWN'

    if catch_all is True:
        catch_all_text = 'TRUE'
    elif catch_all is False:
        catch_all_text = 'FALSE'
    else:
        catch_all_text = 'UNKNOWN'

    status_suffix = (str(raw_status).strip().lower().replace(' ', '_') if raw_status else '')
    if not status_suffix:
        status_suffix = 'deliverable' if smtp_ok == 'ACCEPT' else 'undeliverable' if smtp_ok == 'REJECT' else 'unknown'
    return smtp_ok, catch_all_text, f'verifier_api:{status_suffix}'


def verifier_api_lookup(email, api_url, api_token=''):
    """Query a self-hosted verifier API. Defaults to Trumail-style GET semantics."""
    base = (api_url or '').strip()
    if not base:
        return None

    params = {}
    request_url = base
    if '{email}' in base or '{token}' in base:
        request_url = base.format(email=email, token=api_token or '')
    else:
        request_url = base.rstrip('/')
        if '/v2/lookups/' not in request_url and not request_url.endswith('/verify') and not request_url.endswith('/check'):
            request_url = request_url + '/v2/lookups/json'
        params['email'] = email
        if api_token:
            params['token'] = api_token

    resp = requests.get(request_url, params=params or None, timeout=10)
    resp.raise_for_status()
    return parse_verifier_api_result(resp.json())

def verify_email_domain(email):
    """Check if domain can receive email: MX record first, then A record fallback (RFC 5321 §5)."""
    try:
        domain = email.split('@', 1)[1]
    except Exception:
        return False
    # Try MX records first
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        if len(answers) > 0:
            return True
    except Exception:
        pass
    # RFC 5321: if no MX record, fall back to A record — the domain itself acts as mail server
    # This is common for small businesses that use hosted email (Gmail, Outlook)
    try:
        answers = dns.resolver.resolve(domain, 'A')
        if len(answers) > 0:
            return True
    except Exception:
        pass
    return False

def is_generic(email):
    try:
        local = email.split('@', 1)[0].lower()
    except Exception:
        return True
    return local in GENERIC_LOCAL

def domain_age_days(domain):
    try:
        w = whois.whois(domain)
        cd = w.creation_date
        # creation_date can be list or datetime
        if isinstance(cd, list):
            cd = cd[0]
        if not cd:
            return None
        if isinstance(cd, str):
            try:
                cd = datetime.fromisoformat(cd)
            except Exception:
                return None
        delta = datetime.now(UTC) - cd
        return delta.days
    except Exception:
        return None

def smtp_check(email, from_address='verify@example.com', attempts=1):
    """Attempt SMTP RCPT check and catch-all detection.

    Tries the primary MX host first, then up to 2 secondary MX hosts if the
    primary fails at the TCP transport layer (RFC 5321 §5 priority ordering).
    For each host, probes ports 25 then 587.

    Returns: (smtp_ok, catch_all_flag, smtp_status)
        smtp_ok:        'ACCEPT' | 'REJECT' | 'UNKNOWN'
        catch_all_flag: 'TRUE'   | 'FALSE'  | 'UNKNOWN'
        smtp_status:    diagnostic string — one of:

        accept_not_catchall    — 250 on target, 5xx on random (confirmed valid)
        accept_catchall        — 250 on both target and random (catch-all domain)
        accept_catchall_unknown — target accepted; random probe inconclusive
        reject_target          — 5xx on RCPT TO (hard mailbox reject)
        soft_defer_4xx         — 4xx response (transient; UNKNOWN, not REJECT)
        transport_blocked      — TCP failure on all MX hosts/ports tried;
                                 probe never reached SMTP layer — NOT a rejection
        mx_lookup_failed       — Cannot resolve MX records via DNS
        probe_failed           — Session started but exchange failed unexpectedly
        invalid_email          — email missing '@'
    """
    try:
        domain = email.split('@', 1)[1]
    except Exception:
        return 'UNKNOWN', 'UNKNOWN', 'invalid_email'

    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        # Sort ascending by preference (lowest = highest priority).
        # Try at most 3 hosts: secondary MX hosts are backup relays and may not
        # apply the same reputation-based blocking as the primary.
        mx_hosts = [
            r.exchange.to_text().rstrip('.')
            for r in sorted(mx_records, key=lambda r: r.preference)[:3]
        ]
    except Exception:
        # RFC 5321 §5 fallback: no MX records — try domain's A record as SMTP host.
        try:
            dns.resolver.resolve(domain, 'A')
            mx_hosts = [domain]
        except Exception:
            return 'UNKNOWN', 'UNKNOWN', 'mx_lookup_failed'
    def _try_one(host, port, addr):
        """Connect to host:port, probe addr via RCPT TO.

        Returns (smtp_code_or_none, is_transport_error):
            (int,  False) → received an SMTP response code
            (None, True)  → TCP-level failure (port blocked / timeout / refused)
            (None, False) → unexpected exception mid-session
        """
        server = None
        try:
            # Keep socket timeout low so cloud-egress blocks fail quickly instead
            # of stalling entire daily runs across many permutations.
            server = smtplib.SMTP(timeout=3)
            server.set_debuglevel(0)
            server.connect(host, port)
            server.helo(server.local_hostname)
            server.mail(from_address)
            code, _ = server.rcpt(addr)
            return int(code), False
        except smtplib.SMTPResponseException as e:
            # An SMTP-layer error code was received — not a transport failure.
            return int(e.smtp_code), False
        except smtplib.SMTPConnectError:
            return None, True   # TCP connect to SMTP port failed
        except (socket.timeout, ConnectionRefusedError, OSError):
            return None, True   # port blocked, firewall, or routing failure
        except Exception:
            return None, False  # unexpected; not clearly a transport error
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

    def _probe_all_hosts(addr):
        """Probe addr across all MX hosts and fallback ports.

        Stops at the first host+port that returns an SMTP response code.
        Returns (code_or_none, all_transport_blocked).
        all_transport_blocked is True only when every attempt was a TCP error.
        """
        all_transport = True
        for host in mx_hosts:
            for port in SMTP_PROBE_PORTS:
                code, is_transport = _try_one(host, port, addr)
                if is_transport:
                    continue  # try next port / host
                # Got an SMTP response (or an unexpected non-transport exception).
                # Either way, we reached the SMTP layer on this host+port.
                all_transport = False
                return code, False
        return None, all_transport

    # Probe target address with retry + backoff (handles transient greylisting).
    tgt_code = None
    tgt_blocked = False
    for attempt in range(attempts):
        code, blocked = _probe_all_hosts(email)
        if code is not None:
            tgt_code = code
            tgt_blocked = False
            break
        tgt_blocked = blocked
        if attempt < attempts - 1:
            time.sleep(0.5 * (attempt + 1))

    if tgt_code is None:
        if tgt_blocked:
            # Every MX host + port pair hit a TCP-level error.
            # The probe never reached the SMTP layer — this is NOT a rejection.
            return 'UNKNOWN', 'UNKNOWN', 'transport_blocked'
        return 'UNKNOWN', 'UNKNOWN', 'probe_failed'

    # 4xx = transient deferral: RFC 5321 §4.2.1 requires senders to retry.
    # In verification context: the server is alive but temporarily unavailable.
    # Classify as UNKNOWN (not REJECT) — a 4xx is categorically different from 5xx.
    if 400 <= tgt_code < 500:
        return 'UNKNOWN', 'UNKNOWN', 'soft_defer_4xx'

    # 5xx = permanent rejection: the server explicitly refuses this mailbox.
    if tgt_code >= 500:
        return 'REJECT', 'UNKNOWN', 'reject_target'

    # 2xx = server accepted RCPT TO — check whether domain is catch-all.
    if tgt_code in (250, 251):
        rand_local = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        rand_email = f"{rand_local}@{domain}"
        rand_code, _ = _probe_all_hosts(rand_email)
        if rand_code is not None and rand_code in (250, 251):
            return 'ACCEPT', 'TRUE', 'accept_catchall'
        if rand_code is not None and rand_code >= 500:
            return 'ACCEPT', 'FALSE', 'accept_not_catchall'
        # Random probe inconclusive (4xx, transport blocked, or unexpected).
        # Target was accepted; catch-all status cannot be determined.
        return 'ACCEPT', 'UNKNOWN', 'accept_catchall_unknown'

    return 'UNKNOWN', 'UNKNOWN', 'probe_failed'


def score_email(email, source='', smtp=False, smtp_cache=None,
                verifier_api_url='', verifier_api_token='', verifier_cache=None,
                mx_checker=None, age_lookup=None, smtp_checker=None, verifier_lookup=None):
    mx_checker = mx_checker or verify_email_domain
    age_lookup = age_lookup or domain_age_days
    smtp_checker = smtp_checker or smtp_check
    verifier_lookup = verifier_lookup or verifier_api_lookup
    if is_generic(email):
        return 'REJECT — generic address', None, None, '', '', 'generic_local', False
    mx = mx_checker(email)
    domain = email.split('@',1)[1] if '@' in email else ''
    age = None
    try:
        age = age_lookup(domain)
    except Exception:
        age = None
    smtp_ok = ''
    catch_all = ''
    smtp_status = 'not_attempted'
    smtp_attempted = False
    if smtp and mx:
        cache_key = domain.lower().strip()
        if smtp_cache is not None and cache_key in smtp_cache:
            smtp_ok, catch_all, smtp_status = smtp_cache[cache_key]
            # This row reused a same-domain probe result.
            smtp_attempted = True
        else:
            smtp_attempted = True
            try:
                smtp_ok, catch_all, smtp_status = smtp_checker(email)
                # Cache only transport/infrastructure outcomes that are domain-level,
                # not mailbox-level (do not cache ACCEPT/REJECT target results).
                if smtp_cache is not None and smtp_status in (
                    'transport_blocked', 'mx_lookup_failed', 'probe_failed', 'soft_defer_4xx'
                ):
                    smtp_cache[cache_key] = (smtp_ok, catch_all, smtp_status)
            except Exception:
                smtp_ok, catch_all, smtp_status = 'UNKNOWN', 'UNKNOWN', 'probe_exception'
    elif smtp and not mx:
        smtp_status = 'skipped_no_mx'

    if mx and verifier_api_url and (
        not smtp or smtp_status in ('not_attempted', 'transport_blocked', 'mx_lookup_failed', 'probe_failed', 'soft_defer_4xx', 'probe_exception')
    ):
        cache_key = email.lower().strip()
        api_result = None
        if verifier_cache is not None and cache_key in verifier_cache:
            api_result = verifier_cache[cache_key]
        else:
            try:
                api_result = verifier_lookup(email, verifier_api_url, verifier_api_token)
                if verifier_cache is not None and api_result is not None:
                    verifier_cache[cache_key] = api_result
            except Exception:
                api_result = ('UNKNOWN', 'UNKNOWN', 'verifier_api:error')
        if api_result is not None:
            api_smtp_ok, api_catch_all, api_status = api_result
            if api_smtp_ok != 'UNKNOWN' or api_catch_all != 'UNKNOWN' or api_status != 'verifier_api:unknown':
                smtp_ok, catch_all, smtp_status = api_smtp_ok, api_catch_all, api_status
                smtp_attempted = True

    if not mx:
        return 'REJECT — dead domain', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted

    # Officer permutation guardrail:
    # When SMTP was attempted, require explicit acceptance for PASS.
    # When SMTP was NOT attempted (default MX-only mode), allow PASS with
    # an 'UNVERIFIED' tag so the scorer can differentiate.
    src = (source or '').strip().lower()
    if src == 'officer_permutation':
        smtp_ok_u = (smtp_ok or '').strip().upper()
        catch_all_u = (catch_all or '').strip().upper()
        if smtp and smtp_attempted:
            if smtp_ok_u == 'ACCEPT' and catch_all_u == 'FALSE':
                return 'PASS', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted
            if smtp_ok_u == 'REJECT':
                return 'REJECT — mailbox rejected', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted
            # Transport blocked or inconclusive — not a rejection, but unverified
            return 'UNVERIFIED — smtp inconclusive', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted
        else:
            # SMTP not attempted — MX passed, so email domain is valid.
            # Mark as PASS so it flows through; scorer will cap confidence at C.
            return 'PASS', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted

    if (smtp_ok or '').strip().upper() == 'REJECT':
        return 'REJECT — mailbox rejected', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted

    return 'PASS', mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted

def verify(input_csv, output_csv, smtp=False, verifier_api_url='', verifier_api_token=''):
    with open(input_csv, newline='', encoding='utf-8') as fin, open(output_csv, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames + ['mx_pass', 'reject_reason', 'verification_score', 'domain_age_days', 'smtp_ok', 'catch_all', 'smtp_status', 'smtp_attempted']
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        smtp_cache = {}
        verifier_cache = {}
        for row in reader:
            email = row.get('email', '').strip()
            if not email:
                row['mx_pass'] = 'FALSE'
                row['reject_reason'] = 'Missing email'
                row['verification_score'] = 'REJECT — missing'
                row['domain_age_days'] = ''
                row['smtp_ok'] = ''
                row['catch_all'] = ''
                row['smtp_status'] = 'missing_email'
                row['smtp_attempted'] = 'FALSE'
                writer.writerow(row)
                continue

            score, mx, age, smtp_ok, catch_all, smtp_status, smtp_attempted = score_email(
                email,
                source=row.get('source', ''),
                smtp=smtp,
                smtp_cache=smtp_cache,
                verifier_api_url=verifier_api_url,
                verifier_api_token=verifier_api_token,
                verifier_cache=verifier_cache,
            )
            row['verification_score'] = score
            row['domain_age_days'] = age if age is not None else ''
            row['smtp_ok'] = smtp_ok
            row['catch_all'] = catch_all
            row['smtp_status'] = smtp_status
            row['smtp_attempted'] = 'TRUE' if smtp_attempted else 'FALSE'
            # Preserve the domain-level MX check independently of mailbox verdict.
            row['mx_pass'] = 'TRUE' if bool(mx) else 'FALSE'
            if score.startswith('PASS'):
                row['reject_reason'] = ''
            else:
                row['reject_reason'] = score
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', '-i', required=True)
    parser.add_argument('--output', '-o', required=True)
    parser.add_argument('--smtp', action='store_true', help='Enable SMTP RCPT checks (may be blocked by mail servers)')
    parser.add_argument('--verifier-api-url', default='', help='Optional self-hosted verifier API URL (Trumail-style by default)')
    parser.add_argument('--verifier-api-token', default='', help='Optional token for the verifier API')
    args = parser.parse_args()
    verify(args.input, args.output, smtp=args.smtp,
           verifier_api_url=args.verifier_api_url,
           verifier_api_token=args.verifier_api_token)

if __name__ == '__main__':
    main()
