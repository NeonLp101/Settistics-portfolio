// Downloads Riot Data Dragon assets at build time so visitors' browsers never contact Riot's CDN
// (no visitor IP addresses are sent to third parties; GDPR Art. 6/44).
import {mkdir, readFile, writeFile, access} from 'node:fs/promises';

const DD = 'https://ddragon.leagueoflegends.com';

async function get(url, binary = false) {
  const res = await fetch(url, {signal: AbortSignal.timeout(20000)});
  if (!res.ok) throw new Error(`Data Dragon returned ${res.status} for ${url}`);
  return binary ? Buffer.from(await res.arrayBuffer()) : res.json();
}

export async function fetchStatic(dir) {
  const file = `${dir}/static.json`;
  let cached = null;
  try { cached = JSON.parse(await readFile(file, 'utf8')); } catch {}
  let version;
  try { [version] = await get(`${DD}/api/versions.json`); }
  catch (error) {
    if (cached) { console.warn(`Data Dragon unreachable; keeping cached ${cached.version}.`); return cached; }
    throw error;
  }
  if (cached?.version === version && cached.completedItems && cached.itemIcons && cached.runeTree && cached.guideIcons && cached.shardNames) return cached;

  const cdn = `${DD}/cdn/${version}`;
  const [champs, runes, spells, items] = await Promise.all([
    get(`${cdn}/data/en_US/champion.json`), get(`${cdn}/data/en_US/runesReforged.json`), get(`${cdn}/data/en_US/summoner.json`),
    get(`${cdn}/data/en_US/item.json`),
  ]);
  // Finished items: legendaries (nothing builds from them, 2000+ gold) and upgraded boots.
  const completedItems = Object.entries(items.data).filter(([, it]) => {
    const tags = it.tags || [];
    if (tags.includes('Consumable') || tags.includes('Trinket') || it.maps?.['11'] === false) return false;
    return tags.includes('Boots') ? Boolean(it.from?.length) : !it.into?.length && (it.gold?.total || 0) >= 2000;
  }).map(([id]) => id);
  const champions = Object.values(champs.data)
    .map(c => ({id: c.id, key: c.key, name: c.name, title: c.title, tags: c.tags, partype: c.partype, attackRange: c.stats?.attackrange || 0}))
    .sort((a, b) => a.name.localeCompare(b.name));
  // runeTree: rune id -> [tree id, row], so pages can be listed in the order the client shows them.
  const runeNames = {}, runeTree = {}, runeIcons = {}, spellIcons = {};
  const artwork = [];
  for (const style of runes) {
    runeNames[style.id] = style.name;
    runeIcons[style.id] = `rune/${style.id}.png`;
    artwork.push([`rune/${style.id}.png`, `${DD}/cdn/img/${style.icon}`]);
    style.slots.forEach((slot, row) => { for (const r of slot.runes) {
      runeNames[r.id] = r.name; runeTree[r.id] = [style.id, row];
      runeIcons[r.id] = `rune/${r.id}.png`;
      artwork.push([`rune/${r.id}.png`, `${DD}/cdn/img/${r.icon}`]);
    } });
  }
  // Stat shards are not in runesReforged.json; their ids and icons are fixed.
  const SHARDS = {5008:['Adaptive Force','StatModsAdaptiveForceIcon'],5005:['Attack Speed','StatModsAttackSpeedIcon'],5007:['Ability Haste','StatModsCDRScalingIcon'],
    5010:['Move Speed','StatModsMovementSpeedIcon'],5001:['Health Scaling','StatModsHealthScalingIcon'],5011:['Health','StatModsHealthPlusIcon'],5013:['Tenacity and Slow Resist','StatModsTenacityIcon']};
  const shardNames = {};
  for (const [id,[name,icon]] of Object.entries(SHARDS)) {
    shardNames[id] = name; runeIcons[id] = `rune/${id}.png`;
    artwork.push([`rune/${id}.png`, `${DD}/cdn/img/perk-images/StatMods/${icon}.png`]);
  }
  const spellNames = Object.fromEntries(Object.values(spells.data).map(s => [s.key, s.name]));
  for (const s of Object.values(spells.data)) {
    spellIcons[s.key] = `spell/${s.key}.png`;
    artwork.push([`spell/${s.key}.png`, `${cdn}/img/spell/${s.image.full}`]);
  }

  await mkdir(`${dir}/champion`, {recursive: true});
  for (let i = 0; i < champions.length; i += 16) {
    await Promise.all(champions.slice(i, i + 16).map(async c =>
      writeFile(`${dir}/champion/${c.id}.png`, await get(`${cdn}/img/champion/${c.id}.png`, true))));
  }
  await mkdir(`${dir}/item`, {recursive: true});
  const itemIds = Object.keys(items.data);
  for (const id of itemIds) artwork.push([`item/${id}.png`, `${cdn}/img/item/${id}.png`]);
  await mkdir(`${dir}/rune`, {recursive:true});
  await mkdir(`${dir}/spell`, {recursive:true});
  for (let i = 0; i < artwork.length; i += 16) {
    await Promise.all(artwork.slice(i, i + 16).map(async ([name,url]) => {
      try { await access(`${dir}/${name}`); return; } catch {}
      await writeFile(`${dir}/${name}`, await get(url, true));
    }));
  }
  const itemNames = Object.fromEntries(Object.entries(items.data).map(([id,it])=>[id,it.name]));
  const out = {version, champions, runeNames, runeTree, runeIcons, shardNames, spellNames, spellIcons, itemNames, itemIds, completedItems, itemIcons: true, guideIcons:true, source: 'Riot Data Dragon', fetchedAt: new Date().toISOString()};
  await writeFile(file, JSON.stringify(out));
  return out;
}
