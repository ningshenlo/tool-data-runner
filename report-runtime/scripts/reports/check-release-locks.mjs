import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
const root=process.cwd();
const directory=path.join(root,'content/reports/releases');
const locks=await fs.readdir(directory).catch(error=>{if(error.code==='ENOENT')return [];throw error;});
for(const name of locks.filter(name=>name.endsWith('.json'))) {
  const release=JSON.parse(await fs.readFile(path.join(directory,name),'utf8'));
  for(const [relative,expected] of Object.entries(release.sha256)) {
    const absolute=path.resolve(root,relative);
    if(!absolute.startsWith(root+path.sep))throw Error('Invalid release path');
    const actual=createHash('sha256').update(await fs.readFile(absolute)).digest('hex');
    if(actual!==expected)throw Error(`Published edition changed: ${relative}. Preserve the original and create a correction version.`);
  }
}
for(const name of (await fs.readdir(path.join(root,'content/reports'))).filter(name=>/\.v\d+\.json$/.test(name))) {
  const report=JSON.parse(await fs.readFile(path.join(root,'content/reports',name),'utf8'));
  if(report.publication.status==='published'&&!locks.includes(`${report.id}.${report.version}.json`))throw Error('Published edition has no release lock: '+report.id);
}
console.log(`Verified ${locks.length} immutable report releases.`);
