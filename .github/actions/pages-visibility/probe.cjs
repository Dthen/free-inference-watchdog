// Diagnostic only: one runner-scoped listing, no deploy or public REST calls.
function classify(artifacts, expectedName, expectedId) {
  const matches = artifacts.filter(artifact => artifact.name === expectedName);
  if (matches.length === 0) return 'missing';
  if (matches.length > 1) return 'duplicate';
  return String(matches[0].id) === expectedId ? 'visible' : 'wrong-id';
}

async function main() {
  const {DefaultArtifactClient} = require('@actions/artifact');
  const phase = process.env.INPUT_PHASE;
  const expectedName = process.env.INPUT_ARTIFACT_NAME;
  const expectedId = process.env.INPUT_ARTIFACT_ID;
  if (!['immediate', 'dependent'].includes(phase) ||
      !/^pages-visibility-probe-[0-9]+-[0-9]+-[0-9]+$/.test(expectedName) ||
      !/^[1-9][0-9]*$/.test(expectedId)) {
    throw new Error('invalid or absent artifact identity');
  }
  const started = Date.now();
  // Same SDK version, no findBy or filtering options, as the current Pages action.
  const {artifacts} = await new DefaultArtifactClient().listArtifacts();
  const result = classify(artifacts, expectedName, expectedId);
  const matches = artifacts.filter(artifact => artifact.name === expectedName);
  console.log('PAGES_PROBE ' + JSON.stringify({phase, name: expectedName, expectedId,
    matchingIds: matches.map(artifact => String(artifact.id)),
    result, lookupMs: Date.now() - started}));
  // An immediate miss is a measured result, not a failure that skips its paired job.
  // Wrong identity, duplicate artifacts, or a dependent miss fail the sample.
  if (result !== 'visible' && !(phase === 'immediate' && result === 'missing')) {
    process.exitCode = 1;
  }
}

module.exports = {classify};
if (require.main === module) {
  main().catch(() => {
    // Do not dump SDK exceptions or environment into the public log.
    console.error('PAGES_PROBE_ERROR: artifact lookup or setup failed');
    process.exitCode = 1;
  });
}
