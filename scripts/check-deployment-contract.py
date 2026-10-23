"""Validate rendered production Compose without contacting production resources."""
import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--sitemap-replicas', type=int, required=True)
args = parser.parse_args()
config = json.load(sys.stdin)
services = config['services']
addresses = {'assets-worker': '172.22.0.21', 'periodic-facts-worker': '172.22.0.22',
             'taxonomy-worker': '172.22.0.24', 'sitemap-monitor-worker': '172.22.0.25',
             'report-review-api': '172.22.0.20'}
assert set(services) == set(addresses) | {'report-draft-worker'}
for name, address in addresses.items():
    service = services[name]
    assert service['environment']['DATABASE_BACKEND'] == 'postgres', name
    assert service['environment']['DATABASE_URL'].startswith('postgresql://'), name
    assert service['networks']['sigpik_pg']['ipv4_address'] == address, name
    ca = next(v for v in service['volumes'] if v['target'] == '/run/pg-ca')
    assert ca['source'] == 'sigpik_pg_public_ca' and ca['read_only'], name
    assert ca['volume']['subpath'] == 'sigpik-public-ca', name
    assert service['build']['dockerfile'] == 'Dockerfile', name
assert config['networks']['sigpik_pg'] == {'name': 'ainav-pg-vpc-test-20261019', 'external': True}
assert config['networks']['default']['name'] == 'service-sigpik-h4m21a_default'
for name in ('report-exports', 'report-drafts', 'market-snapshots'):
    assert config['volumes'][name]['name'] == 'sigpik-' + name
assert config['volumes']['sigpik_pg_public_ca'] == {'name': 'ainav-pigsty-test-dnkttj_pigsty_data', 'external': True}
assert services['report-draft-worker']['network_mode'] == 'none'
assert services['report-draft-worker']['read_only']
assert services['sitemap-monitor-worker']['deploy']['replicas'] == args.sitemap_replicas
assert services['sitemap-monitor-worker']['environment']['SITEMAP_MONITOR_ENABLED'] == str(args.sitemap_replicas)
print('Production PostgreSQL, network, volume and service-switch contracts passed.')
