import { build } from 'esbuild'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdirSync, writeFileSync } from 'node:fs'

if (!process.argv[2]) throw new Error('Usage: node build-active-ui-fixture.mjs OUTPUT_DIRECTORY')
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..'),out=resolve(process.argv[2])
mkdirSync(resolve(out,'dist'),{recursive:true})
writeFileSync(resolve(out,'package.json'),'{"type":"module"}')
await build({
  entryPoints:[resolve(root,'scripts/active-ui-fixture.tsx')],bundle:true,platform:'node',format:'esm',target:'node20',
  outfile:resolve(out,'dist/entry.js'),jsx:'automatic',jsxImportSource:'react',
  alias:{'@hermes/ink':resolve(root,'packages/hermes-ink/src/entry-exports.ts')},
  plugins:[{name:'test-devtools-stub',setup(b){
    b.onResolve({filter:/^react-devtools-core$/},()=>({path:'devtools',namespace:'stub'}))
    b.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:'export default {initialize(){},connectToDevTools(){}}'}))
  }}],
  banner:{js:"import {createRequire as __cr} from 'node:module';const require=__cr(import.meta.url);"}
})
console.log(`Built deterministic fixture at ${out}`)
