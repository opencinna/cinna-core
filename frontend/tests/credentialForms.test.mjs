import assert from 'node:assert/strict'
import path from 'node:path'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { chromium, expect } from '@playwright/test'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react-swc'
import { createServer } from 'vite'
const root=fileURLToPath(new URL('..',import.meta.url))
let server,browser,baseUrl,html
before(async()=>{
 server=await createServer({configFile:false,root,resolve:{alias:{'@':path.join(root,'src')}},plugins:[react(),tailwindcss()],server:{host:'127.0.0.1',port:0},logLevel:'error'})
 await server.listen();baseUrl=`http://127.0.0.1:${server.httpServer.address().port}`
 html=await server.transformIndexHtml('/','<html><body><div id="root"></div><script type="module" src="/tests/fixtures/credentialFormsHarness.tsx"></script></body></html>')
 browser=await chromium.launch({headless:true})
})
after(async()=>{await browser?.close();await server?.close()})
for(const type of ['api_token','email_imap','email_smtp','odoo','google_service_account','gmail_oauth','gmail_oauth_readonly','gdrive_oauth','gdrive_oauth_readonly','gcalendar_oauth','gcalendar_oauth_readonly','ssh_key','agent_api','agent_api_key','mcp_provider']) {
 test(`${type} edits and saves Service URI as shared credential metadata`,async t=>{
  const page=await browser.newPage({viewport:{width:1100,height:850}});t.after(()=>page.close());page.setDefaultTimeout(10000)
  const writes=[],errors=[];page.on('pageerror',e=>errors.push(e.message))
  await page.route(`${baseUrl}/?*`,route=>route.fulfill({contentType:'text/html',body:html}))
  await page.route('**/api/**',async route=>{
   const request=route.request(),url=new URL(request.url()).pathname
   if(request.method()==='PUT') {writes.push(request.postDataJSON());return route.fulfill({json:{id:'credential-1',...request.postDataJSON()}})}
   let json={data:[],count:0}
   if(url.includes('oauth'))json={is_authorized:false,scopes:[]}
   if(url.includes('connection'))json={consumer_agents:[],linked_agents:[],linked_count:0,credential_id:'credential-1',status:'connected',modes:[]}
   if(url.endsWith('/producer-1'))json={id:'producer-1',name:'Fixture agent'}
   return route.fulfill({json})
  })
  await page.goto(`${baseUrl}/?type=${type}`)
  const field=page.getByLabel('Service URI',{exact:true});try { await expect(field).toHaveValue('existing-service') } catch(error) { console.error(await page.locator('body').innerText()); throw error }; await field.fill(`service-${type}`)
  await page.getByRole('button',{name:'About Service URI'}).hover()
  await expect(page.getByRole('tooltip')).toContainText('credential type')
  if(type==='email_imap')await page.screenshot({path:'/tmp/cinna-core-service-uri.png'})
  await page.getByRole('button',{name:/^Save(?: Changes| changes)?$/}).click()
  await expect.poll(()=>writes.length).toBe(1)
  assert.equal(writes[0].service_uri,`service-${type}`)
  assert.deepEqual(errors,[])
 })
}
