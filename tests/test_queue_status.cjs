const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
test('queue indicator stays red until every job succeeds, errors never turn green', async()=>{
 const dot={dataset:{},setAttribute(k,v){this[k]=v}};
 const timers=[];
 const context=vm.createContext({$:()=>dot,api:()=>new Promise(()=>{}),setTimeout:fn=>{timers.push(fn);return timers.length},clearTimeout(){},setInterval(){}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/quire/server/web/queue-status.js'),'utf8'),context);
 context.updateQueueJob({id:'a',status:'running'});
 context.updateQueueJob({id:'b',status:'pending'});
 assert.equal(dot.dataset.status,'active');assert.equal(dot.hidden,false);
 context.updateQueueJob({id:'a',status:'done'});
 assert.equal(dot.dataset.status,'active');
 context.updateQueueJob({id:'b',status:'done'});
 assert.equal(dot.dataset.status,'done');assert.equal(dot.hidden,false);
 timers.at(-1)();assert.equal(dot.hidden,true);
 context.updateQueueJob({id:'c',status:'running'});assert.equal(dot.hidden,false);
 context.updateQueueJob({id:'c',status:'failed'});assert.equal(dot.dataset.status,'idle');assert.equal(dot.hidden,true);
});

test('queue poll uses short active interval and backs off when idle', async () => {
 const dot={dataset:{},setAttribute(k,v){this[k]=v}};
 const timers=[]; let jobs=[{id:'a',status:'running'}];
 const context=vm.createContext({$:()=>dot,api:async()=>({jobs}),setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout(){}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/quire/server/web/queue-status.js'),'utf8'),context);
 await new Promise(setImmediate);
 assert.equal(timers.at(-1).delay,3000);
 jobs=[{id:'a',status:'done'}];
 await context.refreshQueueStatus();
 assert.equal(timers.at(-1).delay,30000);
});
