// Not-yet-implemented C6 contract. Kept separate from the must-pass gate:
// `python3 scripts/ci.py contracts` reports it without masking the gap.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
async function auth(request,user){expect((await request.post('/api/auth/login',{data:{username:user,password:user==='admin'?'fixture-browser-password':'fixture-member-password'}})).status()).toBe(200);}
test('C6 AIHO-19 developer can open chat UI',async({page})=>{await auth(page.request,'developer');await page.goto('/chat');await expect(page.locator('textarea')).toBeVisible({timeout:3000});});
