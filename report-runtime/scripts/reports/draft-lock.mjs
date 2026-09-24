import fs from 'node:fs/promises';
import os from 'node:os';

async function processStart(pid) {
  if (process.platform !== 'linux') return null;
  const stat = await fs.readFile(`/proc/${pid}/stat`, 'utf8');
  return (await fs.readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim() + ':' + stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19];
}

/** Recover only a provably dead local owner. Live/foreign/malformed locks fail closed. */
export async function acquireDraftLock(file) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const handle = await fs.open(file, 'wx');
      await handle.writeFile(JSON.stringify({ pid: process.pid, host: os.hostname(), processStart: await processStart(process.pid), startedAt: new Date().toISOString() }));
      return handle;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const bytes = await fs.readFile(file, 'utf8'), owner = JSON.parse(bytes);
      let dead = false;
      if (owner.host === os.hostname() && Number.isSafeInteger(owner.pid) && owner.pid > 0) {
        try {
          process.kill(owner.pid, 0);
          dead = !!owner.processStart && owner.processStart !== await processStart(owner.pid);
        } catch (error) {
          if (['ESRCH', 'ENOENT'].includes(error.code)) dead = true;
          else throw error;
        }
      }
      if (!dead || attempt) throw Error(`Another generation may be running; inspect ${file}`);
      if (await fs.readFile(file, 'utf8') !== bytes) throw Error('Draft lock owner changed');
      await fs.unlink(file);
    }
  }
}
