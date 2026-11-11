#!/usr/bin/env node
import { main } from '../src/cli.js'

main(process.argv.slice(2)).catch((error) => {
  console.error(`fedcap: ${error instanceof Error ? error.message : String(error)}`)
  process.exit(1)
})
