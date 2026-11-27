// Each wallet page has a fixed source position, amount, destination range and nonce sequence.
const owner = '0xb6bf0ba3f43d4386216df037dca1a1203038e135';
const manager = '0x1238536071e1c677a632429e3655c799b22cda52';
const quote = '0xc319f958df8a74b077578b437c402dc7d7687864';
const base = '0xe00296a373cd19e6abaf085e50a393938879737d';
const plans = {
  'lp-half.html': {initial:63245875460548078554n, remaining:31622937730274039277n, ticks:[-69180,-69000], quoteCap:790n, quoteMin:783n, nonce:134, action:'Move half and mint new LP', result:'Half the original liquidity remains in #234996.'},
  'lp-tight.html': {initial:31622937730274039277n, remaining:0n, ticks:[-69120,-69060], quoteCap:515n, quoteMin:505n, nonce:139, action:'Move remaining full-range liquidity', result:'All remaining full-range liquidity was removed from #234996.'},
  'lp-dip.html': {walletOnly:true, ticks:[-69060,-69000], quoteCap:700n, nonce:142, action:'Mint the 700 f564 dip position', result:'Your dip position is ready.'}
};
const plan = plans[location.pathname.split('/').pop()];
const chain = '0xaa36a7', tokenId = 234996n, original = plan.initial;
const unit = 10n**18n, desired0 = plan.quoteCap*unit, desired1 = plan.walletOnly ? 0n : 999n*unit/1000n;
const min0 = plan.walletOnly ? desired0-1n : plan.quoteMin*unit, min1 = plan.walletOnly ? 0n : 994n*unit/1000n;
const max128 = (1n<<128n)-1n;
const firstNonce = plan.nonce, approvals = plan.walletOnly ? [quote] : [quote,base];
const destinations = [...approvals,manager], mintStep = approvals.length;
const labels = [`Approve ${plan.quoteCap} f564`, ...(!plan.walletOnly ? ['Approve 0.999 fETH'] : []), plan.action];
const key = `phase3-lp:${chain}:${owner}:${plan.walletOnly ? 'dip' : tokenId}:${firstNonce}`;
const $ = id => document.getElementById(id);
const fail = text => {throw Error(text);};
const word = value => BigInt.asUintN(256, BigInt(value)).toString(16).padStart(64,'0');
const call = (selector,...args) => '0x'+selector+args.map(word).join('');
const hex = n => '0x'+n.toString(16);
const get = () => JSON.parse(localStorage.getItem(key)||'null');
const save = state => localStorage.setItem(key,JSON.stringify(state));
const hashOK = h => /^0x[0-9a-f]{64}$/i.test(h || '');
const wallet = (method,params=[]) => window.ethereum.request({method,params});
const words = raw => {
  if(!/^0x(?:[0-9a-f]{64})+$/i.test(raw)) fail('Incomplete contract response. Check status again.');
  return raw.slice(2).match(/.{64}/g).map(w=>BigInt('0x'+w));
};

function multicall(calls){
  const tails = calls.map(data => {
    const bytes = data.slice(2);
    return word(bytes.length/2)+bytes.padEnd(Math.ceil(bytes.length/64)*64,'0');
  });
  let offset = 32*calls.length;
  const heads = tails.map(tail => {const at=offset; offset+=tail.length/2; return word(at);});
  return call('ac9650d8',32,calls.length)+heads.join('')+tails.join('');
}

function request(step,deadline){
  if(step<mintStep) return {to:approvals[step],data:call('095ea7b3',manager,[desired0,desired1][step])};
  const mint=call('88316456',quote,base,3000,...plan.ticks,desired0,desired1,min0,min1,owner,deadline);
  if(plan.walletOnly) return {to:manager,data:mint};
  return {to:manager,data:multicall([
    call('0c49ccbe',tokenId,original-plan.remaining,998n*unit,desired1,deadline),
    call('fc6f7865',tokenId,owner,max128,max128),
    mint
  ])};
}

function verifySimulation(step,raw){
  const v=words(raw);
  if(step<mintStep){
    if(v.length!==1 || v[0]!==1n) fail('Approval simulation failed.');
    return;
  }
  if(plan.walletOnly){
    const [id,liquidity,amount0,amount1]=v;
    if(v.length!==4 || !id || !liquidity || amount0<min0 || amount0>desired0 || amount1!==0n) fail('The simulated mint differs from the 700 f564 deposit.');
    return;
  }
  // ABI-encoded bytes[] with three static outputs: decrease, collect, mint.
  if(v.length!==16 || v[0]!==32n || v[1]!==3n || v[2]!==96n || v[3]!==192n || v[4]!==288n || v[5]!==64n || v[8]!==64n || v[11]!==128n) fail('Could not verify the rebalance simulation.');
  const [newId,newL,amount0,amount1]=v.slice(12);
  if(v[6]<998n*unit || v[7]<desired1 || v[9]<v[6] || v[10]<v[7] || !newId || !newL || amount0<min0 || amount0>desired0 || amount1<min1 || amount1>desired1) fail('Simulated amounts differ from the reviewed allocation.');
}

let useBackupUntil=0;
async function backup(method,params){
  const response=await fetch('https://ethereum-sepolia.publicnode.com',{
    method:'POST',credentials:'omit',cache:'no-store',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({jsonrpc:'2.0',id:1,method,params}),signal:AbortSignal.timeout(10000)
  });
  if(!response.ok) fail(`Sepolia returned HTTP ${response.status}. Try Check status again.`);
  const body=await response.json();
  if(body.error || !Object.hasOwn(body,'result')) fail('Sepolia could not simulate or read this request. Check the allowances and current price.');
  return body.result;
}
async function read(method,params){
  if(!['eth_getBlockByNumber','eth_getTransactionCount','eth_getTransactionReceipt','eth_call'].includes(method)) fail('Unknown read request.');
  if(Date.now()>=useBackupUntil){
    try{return await wallet(method,params);}catch(e){
      if(!/429|rate.?limit|too many requests/i.test(`${e.code} ${e.message}`)) throw e;
      if(await backup('eth_chainId',[])!==chain) fail('Unexpected network at backup provider.');
      useBackupUntil=Date.now()+60000;
    }
  }
  return backup(method,params);
}
async function identity(){
  if(!window.ethereum) fail('Open this page in the browser with your wallet extension.');
  const accounts=await wallet('eth_accounts');
  if(accounts[0]?.toLowerCase()!==owner) fail('Connect the enrolled account 0xB6bf…e135.');
  if((await wallet('eth_chainId')).toLowerCase()!==chain) fail('Select Ethereum Sepolia in your wallet.');
}
function receiptLink(hash){
  if(!hashOK(hash)) return;
  $('receipt').href='https://sepolia.etherscan.io/tx/'+hash;
  $('receipt').hidden=false;
}
const transfer='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
function minted(receipt){
  const logs=receipt.logs.filter(l=>l.address.toLowerCase()===manager && l.topics.length===4 && l.topics[0]===transfer && BigInt(l.topics[1])===0n && BigInt(l.topics[2])===BigInt(owner));
  if(logs.length!==1) fail('Review the receipt. Could not verify the new LP NFT.');
  return BigInt(logs[0].topics[3]);
}
const contract=async(to,data,block)=>words(await read('eth_call',[{to,data},block]));
async function checkPosition(id,block,lo,hi){
  const [holder]=await contract(manager,call('6352211e',id),block);
  const p=await contract(manager,call('99fbab88',id),block);
  if(holder!==BigInt(owner) || p.length!==12 || p[2]!==BigInt(quote) || p[3]!==BigInt(base) || p[4]!==3000n || BigInt.asIntN(24,p[5])!==BigInt(lo) || BigInt.asIntN(24,p[6])!==BigInt(hi)) fail('The NFT owner, tokens, fee or range differ from this plan.');
  return p[7];
}

async function next(){
  await identity();
  const pending=get(); let newId;
  if(pending){
    if(!hashOK(pending.hash)) fail('A request is open or its outcome is unknown. Check your wallet Activity before continuing.');
    receiptLink(pending.hash);
    const r=await read('eth_getTransactionReceipt',[pending.hash]);
    if(!r) fail('Waiting for your transaction to confirm. Click Check status after confirmation.');
    if(r.transactionHash.toLowerCase()!==pending.hash.toLowerCase() || r.from.toLowerCase()!==owner || r.to.toLowerCase()!==destinations[pending.step]) fail('Wallet receipt differs from the requested transaction.');
    if(BigInt(r.status)!==1n) fail('Transaction reverted. Review the receipt before preparing another request.');
    if(pending.step===mintStep) newId=minted(r);
  }
  const head=await read('eth_getBlockByNumber',['latest',false]);
  const block=head.number;
  if(pending && !newId){
    // Leave the hash intact across checks and reloads until the next request is recorded.
    if(BigInt(await read('eth_getTransactionCount',[owner,block]))<=BigInt(firstNonce+pending.step)) fail('Waiting for the confirmed request to appear at the current head.');
  }
  if(!plan.walletOnly){
    const liquidity=await checkPosition(tokenId,block,-887220,887220);
    if(liquidity!==(newId ? plan.remaining : original)) fail('The source LP liquidity differs from the expected balance. Check wallet Activity.');
  }
  if(newId){
    if(await checkPosition(newId,block,...plan.ticks)<=0n) fail('Waiting to verify the new NFT.');
    return {done:`Confirmed. New LP NFT #${newId} belongs to your wallet. ${plan.result}`};
  }
  if(plan.walletOnly){
    const [balance]=await contract(quote,call('70a08231',owner),block);
    if(balance<desired0) fail('The enrolled wallet needs 700 f564 to fund this position.');
    const slot=await contract('0x7fe977ba8a5b7b25b8805b3f9456d21d3a4a9934','0x3850c7bd',block);
    if(slot.length!==7 || BigInt.asIntN(24,slot[1])>=BigInt(plan.ticks[0])) fail('The price must be above the range for a one-sided f564 deposit.');
  }
  const confirmed=Number(BigInt(await read('eth_getTransactionCount',[owner,block])));
  const inFlight=Number(BigInt(await read('eth_getTransactionCount',[owner,'pending'])));
  if(inFlight!==confirmed) fail('Your wallet has a pending transaction. Wait for its receipt, then check status.');
  const step=confirmed-firstNonce;
  if(step<0 || step>mintStep) fail('The wallet nonce has moved beyond this plan. Review wallet Activity.');
  for(let i=0;i<step;i++){
    const [allowance]=await contract([quote,base][i],call('dd62ed3e',owner,manager),block);
    if(allowance!==[desired0,desired1][i]) fail('The manager allowance differs from this step. Check wallet Activity.');
  }
  const now=Math.floor(Date.now()/1000), at=Number(BigInt(head.timestamp));
  if(at>now+15 || now-at>120) fail('Waiting for a fresh Sepolia block.');
  return {step,deadline:at+300};
}

let busy=false, generation=0, visible;
async function refresh(){
  if(busy) return;
  const gen=++generation; visible=undefined; $('review').hidden=true;
  $('status').textContent='Checking Sepolia…';
  try{
    const state=await next(); if(gen!==generation) return;
    $('connect').hidden=true;
    if(state.done){$('status').textContent=state.done; return;}
    visible=state.step; $('review').textContent=labels[visible]; $('review').hidden=false;
    $('status').textContent=`Step ${visible+1} of ${labels.length}. Review this request in your wallet.`;
  }catch(e){if(gen===generation) $('status').textContent=e.message;}
}
$('refresh').onclick=refresh;
$('connect').onclick=async()=>{
  try{
    if(!window.ethereum) fail('Open this page in the browser with your wallet extension.');
    await wallet('eth_requestAccounts');
    if((await wallet('eth_chainId')).toLowerCase()!==chain) await wallet('wallet_switchEthereumChain',[{chainId:chain}]);
    await refresh();
  }catch(e){$('status').textContent=e.message;}
};
$('review').onclick=async()=>{
  if(busy || visible===undefined) return;
  const step=visible; busy=true; ++generation; $('review').hidden=true;
  $('status').textContent='Simulating this wallet request…';
  try{
    await navigator.locks.request(key,{ifAvailable:true},async lock=>{
      if(!lock) fail('A rebalance request is open in another tab.');
      const state=await next(); if(state.step!==step) return;
      const tx={...request(step,state.deadline),from:owner,value:'0x0'};
      const simulated=await read('eth_call',[tx,'latest']);
      verifySimulation(step,simulated);
      await identity();
      const previous=get(); save({step}); $('status').textContent='Review the request in your wallet.';
      let hash;
      try{hash=await wallet('eth_sendTransaction',[{...tx,chainId:chain,nonce:hex(firstNonce+step)}]);}
      catch(e){if(e.code===4001) save(previous); throw e;}
      if(!hashOK(hash)) fail('No valid transaction hash. Check wallet Activity.');
      save({step,hash}); receiptLink(hash);
    });
  }catch(e){$('status').textContent=e.message; return;}
  finally{busy=false;}
  await refresh();
};
window.ethereum?.on?.('accountsChanged',refresh);
window.ethereum?.on?.('chainChanged',refresh);
if(window.ethereum) refresh();
