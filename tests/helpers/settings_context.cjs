// Exercise the actual shipped submit handler, including a form named-property collision.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
(async () => {
  let submit;
  const requests = [];
  const form = {
    dataset: {}, action: {name:'action', value:'copy'},
    getAttribute: key => key === 'action' ? '/settings/group-action?slug=default' : null,
    addEventListener: (type, handler) => {if(type === 'submit') submit = handler;},
  };
  const context = {
    FormData: class extends Map {constructor(){super([['version','v1']]);}},
    document: {getElementById: () => null, querySelector: () => null,
      querySelectorAll: selector => selector === '[data-settings-form]' ? [form] : [],
      addEventListener() {}},
    location: {href:'http://test/settings/groups',assign: url => {context.destination=url;}},
    prompt: () => 'test-password', confirm: () => true, alert: value => {throw Error(value);},
    fetch: async (url, options) => {
      requests.push({url,body:Object.fromEntries(options.body),headers:options.headers});
      return requests.length === 1 ? {status:401,headers:{get:()=> '1'}} :
        {status:200,redirected:true,url:'http://test/settings/groups?saved=1'};
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../litradar/web/static/settings.js'),'utf8'),context);
  await submit({preventDefault(){},submitter:{name:'action',value:'copy',dataset:{}}});
  process.stdout.write(JSON.stringify({requests,destination:context.destination,busy:form.dataset.busy || ''}));
})().catch(error => {console.error(error);process.exitCode=1;});
