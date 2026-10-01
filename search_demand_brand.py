"""Port of Sigpik's domain_keyword_direct_v2; only classify missing labels.

Cross-language fixtures pin parity with lib/search-demand/brand-classifier.ts.
Monthly publication preserves every existing label, including manual reviews.
"""
import math
import re
import unicodedata
from urllib.parse import urlsplit

VERSION = 'domain_keyword_direct_v2'
ROUTING = set('app beta chat dashboard go home m mobile my platform studio try use web www'.split())
MIXED = set('alternative alternatives competitor competitors replacement replacements versus vs like'.split())
COMMERCIAL = set('cost costs coupon coupons discount discounts free license licenses plan plans price prices pricing review reviews subscription subscriptions trial trials'.split())


def compact(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).lower()
                   if unicodedata.category(c)[0] in 'LN')


def domain_key(domain):
    host = urlsplit(domain if '://' in domain else 'https://' + domain).hostname or ''
    labels = host.lower().removeprefix('www.').rstrip('.').split('.')
    label = next((label for label in labels[:-1] if label not in ROUTING and len(compact(label)) >= 3), labels[0])
    return compact(label)


def classify(observations):
    results = []
    for row in observations:
        domain, keyword = domain_key(row['normalized_domain']), compact(row['keyword'])
        direct = row.get('direct_share')
        direct = min(1, max(0, direct)) if isinstance(direct, (int, float)) and math.isfinite(direct) else None
        kind = 'none'
        if min(len(domain), len(keyword)) >= 3:
            kind = ('exact' if domain == keyword else 'domain_in_keyword' if domain in keyword
                    else 'keyword_in_domain' if keyword in domain else 'none')
        score = {'exact': .94 + (direct or 0) * .05, 'domain_in_keyword': .72 + (direct or 0) * .18,
                 'keyword_in_domain': .55 + (direct or 0) * .25, 'none': 0}[kind]
        matched = kind == 'exact' or score >= .68
        terms = set(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', row['keyword']).lower()))
        demand_type = ('mixed' if terms & MIXED else 'brand_commercial' if terms & COMMERCIAL
                       else 'brand_navigational') if matched else 'generic' if kind == 'none' else 'unknown'
        confidence = score if matched else .8 if kind == 'none' else 1 - abs(.68 - score)
        confidence = math.floor(min(.99, max(0, confidence)) * 10000 + .5) / 10000
        rank = {'exact': 3, 'domain_in_keyword': 2, 'keyword_in_domain': 1, 'none': 0}[kind]
        result = dict(demand_type=demand_type, matched_tool_id=row['product_id'] if matched else None,
                      classification_method=VERSION + ':' + kind, confidence=confidence)
        results.append(((-int(matched), -rank, -confidence, -row['observed_traffic'],
                         -(direct if direct is not None else -1), row['product_id'], row['normalized_domain']), result))
    if not results:
        raise ValueError('At least one observation is required')
    return min(results, key=lambda item: item[0])[1]
