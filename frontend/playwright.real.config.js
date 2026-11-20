import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'../tests/e2e', testMatch:'*.spec.js', workers:1, retries:0, timeout:30000,
  outputDir:process.env.CI_REPORT+'/playwright-results',
  reporter:[['list'],['html',{outputFolder:process.env.CI_REPORT+'/html',open:'never'}],['json',{outputFile:process.env.CI_REPORT+'/playwright.json'}]],
  use:{baseURL:process.env.E2E_URL, headless:true, trace:'retain-on-failure', screenshot:'only-on-failure'},
});
