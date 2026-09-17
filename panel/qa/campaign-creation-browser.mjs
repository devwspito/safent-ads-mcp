import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {resolve, isAbsolute} from 'node:path';
import {pathToFileURL} from 'node:url';
const modulePath=process.env.QA_PLAYWRIGHT_MODULE;
if(!modulePath||!isAbsolute(modulePath)) throw new Error('Set QA_PLAYWRIGHT_MODULE to your installed local playwright/index.mjs');
const {chromium}=await import(pathToFileURL(modulePath).href);
const output=resolve(process.env.QA_OUTPUT_DIR??'qa-results-campaign');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true});
try {
const context=await browser.newContext({viewport:{width:1280,height:900},reducedMotion:'reduce'});
await context.addCookies([{name:'ads_session',value:'integration-test-composition-token',url:'http://127.0.0.1:5212',httpOnly:true},{name:'ads_csrf',value:'fictitious-browser-csrf',url:'http://127.0.0.1:5212'}]);
const page=await context.newPage(), errors=[], writes=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(['PATCH','POST'].includes(r.method()))writes.push({method:r.method(),path:new URL(r.url()).pathname,body:r.postDataJSON()});});
const info=await (await context.request.get('http://127.0.0.1:5212/qa-info')).json();
assert.equal(info.isolated,true,'Refusing to run without the isolated harness marker');
await page.goto('http://127.0.0.1:5212/qa/campaign-creation.html');
const countsBefore=await (await context.request.get('http://127.0.0.1:5212/qa-counts')).json();
const invalidRow=page.locator(`[data-proposal-id="${info.invalid_id}"]`);
await invalidRow.getByRole('button',{name:'Detalle',exact:true}).click();
await page.getByRole('button',{name:'Completar plan de creación',exact:true}).waitFor();
assert.equal(await invalidRow.getByRole('button',{name:'Aprobar',exact:true}).isDisabled(),true);
await invalidRow.click();await page.keyboard.press('a');await page.keyboard.press('Shift+A');
assert.equal(writes.length,0);
const row=page.locator(`[data-proposal-id="${info.first_id}"]`);
await row.getByRole('button',{name:'Detalle',exact:true}).click();
await page.getByRole('button',{name:'Editar plan de creación',exact:true}).click();
await page.getByLabel('Nombre de campaña').fill('Campaña de prueba · revisión real');
await page.getByLabel('Presupuesto diario (EUR)').fill('25.25');
const contrasts=[];
for(const scheme of ['light','dark']){
  await page.emulateMedia({colorScheme:scheme});
  contrasts.push(...await page.getByRole('dialog').evaluate((dialog,scheme)=>{
    function rgb(value){return value.match(/[\d.]+/g).map(Number).slice(0,3);}
    function luminance(color){return color.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);}
    return ['label','p','h2','input','button'].flatMap(selector=>Array.from(dialog.querySelectorAll(selector)).filter(el=>!el.disabled).map(el=>{
      const style=getComputedStyle(el);let bg=style.backgroundColor,p=el;
      while((bg==='rgba(0, 0, 0, 0)'||bg==='transparent')&&p.parentElement){p=p.parentElement;bg=getComputedStyle(p).backgroundColor;}
      const a=luminance(rgb(style.color)),b=luminance(rgb(bg));return {scheme,element:el.tagName,text:el.textContent.trim().slice(0,35),ratio:Number(((Math.max(a,b)+.05)/(Math.min(a,b)+.05)).toFixed(2))};
    }));
  },scheme));
}
assert.ok(contrasts.every(item=>item.ratio>=4.5),JSON.stringify(contrasts.filter(item=>item.ratio<4.5)));
await page.emulateMedia({colorScheme:'light'});
await page.screenshot({path:resolve(output,'google-1280.png')});
await page.getByRole('button',{name:'Revisar plan',exact:true}).click();
await page.setViewportSize({width:390,height:844});
// Use only keyboard to reach the initially below-fold primary action.
let reached=false;
for(let i=0;i<10;i++){await page.keyboard.press('Tab');if(await page.getByRole('button',{name:'Guardar plan sin aprobar'}).evaluate(el=>el===document.activeElement)){reached=true;break;}}
assert.equal(reached,true);
await page.waitForFunction(()=>{const el=[...document.querySelectorAll('[role="dialog"] button')].find(el=>el.textContent==='Guardar plan sin aprobar');if(!el)return false;const r=el.getBoundingClientRect(),d=el.closest('[role="dialog"]').getBoundingClientRect();return r.y>=d.y&&r.bottom<=d.bottom;});
const ctaRect=await page.getByRole('button',{name:'Guardar plan sin aprobar'}).boundingBox();
await page.screenshot({path:resolve(output,'review-390.png')});
console.log('geometry',JSON.stringify({ctaRect,dialog:await page.getByRole('dialog').boundingBox(),scroll:await page.getByRole('dialog').evaluate(el=>({top:el.scrollTop,height:el.clientHeight,scrollHeight:el.scrollHeight}))}));
assert.ok(ctaRect.y>=24&&ctaRect.y+ctaRect.height<=820,JSON.stringify(ctaRect));
await page.screenshot({path:resolve(output,'review-390.png')});
const endpoint=`http://127.0.0.1:5212/api/v1/proposals/${info.first_id}?business_id=${info.business_id}`;
const detail=await(await context.request.get(endpoint)).json();
const otherPlan=structuredClone(detail.creation_plan);otherPlan.daily_budget.amount='24.00';
const concurrent=await context.request.patch(endpoint,{headers:{'X-CSRF-Token':'fictitious-browser-csrf'},data:{diff_hash:detail.diff.diff_hash,creation_plan:otherPlan}});
assert.equal(concurrent.status(),200,await concurrent.text());
await page.keyboard.press('Enter');
await page.getByRole('alert').filter({hasText:'Conservamos este borrador'}).waitFor();
assert.equal(await page.getByRole('button',{name:'Guardar plan sin aprobar'}).isDisabled(),true);
await page.screenshot({path:resolve(output,'conflict-390.png')});
await page.keyboard.press('Escape');
await page.getByText('24.00 EUR',{exact:true}).waitFor();
await page.getByRole('button',{name:'Editar plan de creación',exact:true}).click();
await page.getByLabel('Presupuesto diario (EUR)').fill('25.25');
await page.getByRole('button',{name:'Revisar plan',exact:true}).click();
await page.getByRole('button',{name:'Guardar plan sin aprobar'}).click();
await page.getByRole('dialog').waitFor({state:'hidden'});
await page.getByText('25.25 EUR',{exact:true}).waitFor();
// Same-plan confirmation must close normally and preserve the hash.
const edited=await(await context.request.get(endpoint)).json();
await page.getByRole('button',{name:'Editar plan de creación',exact:true}).click();
await page.getByRole('button',{name:'Revisar plan',exact:true}).click();
await page.getByRole('button',{name:'Guardar plan sin aprobar'}).click();
await page.getByRole('dialog').waitFor({state:'hidden'});
const unchanged=await(await context.request.get(endpoint)).json();
assert.equal(unchanged.diff.diff_hash,edited.diff.diff_hash);
assert.equal(writes.filter(w=>w.path.endsWith('/approve')).length,0);
const countsAfterEdit=await(await context.request.get('http://127.0.0.1:5212/qa-counts')).json();
assert.deepEqual(countsAfterEdit,countsBefore);
await Promise.all([page.waitForResponse(r=>r.url().endsWith(`/${info.first_id}/approve`)&&r.status()===200),row.getByRole('button',{name:'Aprobar',exact:true}).click()]);
assert.equal(writes.filter(w=>w.path.endsWith('/approve')).length,1);
const approval=writes.find(w=>w.path.endsWith('/approve'));
assert.equal(approval.body.diff_hash,edited.diff.diff_hash);
const countsAfterApprove=await(await context.request.get('http://127.0.0.1:5212/qa-counts')).json();
assert.equal(countsAfterApprove.approvals,countsBefore.approvals+1);
const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
assert.equal(overflow,false);assert.deepEqual(errors,[]);
const result={errors,overflow,keyboardCtaInViewport:reached,ctaRect,contrastMinimum:Math.min(...contrasts.map(c=>c.ratio)),contrasts,countsBefore,countsAfterEdit,countsAfterApprove,writes};
await writeFile(resolve(output,'result.json'),JSON.stringify(result,null,2));
console.log(JSON.stringify(result,null,2));
} finally { await browser.close(); }
