/* AgentBus task forest projection. No external resources; bus records remain authoritative. */
(() => {
  'use strict';
  window.addEventListener('error', e => {
    window.__lastJsError = (e && (e.message || (e.error && e.error.stack))) || String(e);
  });
  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const stateNames = {delivered:'已正式交付',admin_passed:'管理员放行 · 成果未核验',running:'执行心跳已确认',claimed:'已认领 · 心跳待确认',queued:'等待认领',blocked:'受阻 · 需检查',stopped:'已停止',missing:'记录待恢复',awaiting_receipt:'待核对正式回执',awaiting_audit:'待独立复核',rejected:'复核未通过'};
  const groups = [{id:'desktop',title:'桌面交互',subtitle:'工作台与成员体验'}, {id:'skills',title:'扩展技能',subtitle:'技能、计划与成员接入'}, {id:'reliability',title:'可靠防撞',subtitle:'心跳、证据与接力'}, {id:'release',title:'工程打包',subtitle:'集成、验证与发布'}];
  let snapshot = {forest:{roots:[],branches:[],leaves:[]},config:{},members:[],project:{},pending_goals:[]}, online = false;
  let filter = 'all', searchQuery = '', focusedMember = '', selectedLeaf = '', scale = 1, panX = 0, panY = 0;
  let world = {width:1300,height:740}, initializedView = false, fitViewport = false, ready=false, toastTimer, toastAction=null, refreshInFlight = false, lastPresenceAnnouncement='',lastScannedWorkspace='',viewportSize={width:0,height:0};
  let quickstartFingerprint = '', quickstartInFlight = false;
  let drag = null, sidebarDrag = null, lastDialogInvoker = null, dialogVersion = 0;
  let autoDispatchActive=false,autoDispatchTimer=null,autoDispatchInFlight=false,autoDispatchDelay=20000,autoDispatchGeneration=0,assignmentSaving=false,viewportSaveTimer=null;
  const detailDrafts=new Map();
  let mutationGeneration=0;
  let lastRejectedSnapshot=null;

  const MUTATING_ACTIONS = new Set([
    'configure',
    'start_goal',
    'claim_leaf',
    'dispatch_leaf',
    'dispatch_ready',
    'submit_leaf_result',
    'claim_audit',
    'submit_audit',
    'stop',
    'resume',
    'checkpoint_leaf',
    'handoff_leaf',
    'append_leaf',
    'add_leaf',
    'add_task',
    'update_leaf_topology',
    'ensure_quickstart',
    'recovery',
    'set_workspace',
    'pass_leaf',
    'reset_leaf',
    'gc_apply',
    'discover_leaf',
    'lessons_add'
  ]);

  /**
   * 判断指定操作是否为写操作（变更状态或文件），用于确定是否需要加互斥锁或刷新视图。
   * @param {string} action - 操作名称标识（如 'claim_leaf', 'configure'）
   * @param {Object} [payload={}] - 操作携带的载荷参数字典
   * @returns {boolean} 若为变更动作返回 true，只读动作返回 false
   */
  /**
   * 判断指定操作是否为写操作（变更状态或文件），用于确定是否需要加互斥锁或刷新视图。
   * @param {string} action - 操作名称标识（如 'claim_leaf', 'configure'）
   * @param {Object} [payload={}] - 操作携带的载荷参数字典
   * @returns {boolean} 若为变更动作返回 true，只读动作返回 false
   */
  function isMutatingAction(action, payload={}) {
    if (MUTATING_ACTIONS.has(action)) return true;
    if (action === 'import_tree' && payload && payload.confirm === true) return true;
    return false;
  }
  const string = value => value == null ? '' : String(value);
  const taskNumber=leaf=>string(leaf.section_no||'').trim();
  const numberedTitle=leaf=>[taskNumber(leaf),string(leaf.title)].filter(Boolean).join(' · ');
  const assignmentMode=()=>['manual','automatic','autonomous'].includes(snapshot.project.assignment_mode)?snapshot.project.assignment_mode:['manual','automatic','autonomous'].includes(snapshot.config.assignment_mode)?snapshot.config.assignment_mode:'automatic';
  const routeName=value=>({file:'项目文件',cli:'独立 CLI',acp:'ACP 会话',sdk:'SDK 会话'})[value]||'项目文件';
  const canAutoDispatch=(live,project,forest,mode)=>Boolean(live&&project?.configured===true&&mode==='automatic'&&!forest?.stopped);
  const taskStateLabel=leaf=>leaf.state==='delivered'&&leaf.artifact_state==='mismatch'?'已交付 · 产物待核对':leaf.state==='blocked'&&leaf.unmet_dependencies?.length?'等待前序任务':stateNames[leaf.state]||leaf.state||'状态未知';
  const exportError=message=>/template needs a root|(?:no|missing)\s+(?:project\s+)?root|empty\s+(?:task\s+)?tree/i.test(string(message))?'请先创建或导入任务树，再导出模板。':string(message);
  let pendingTransitions=new Map();
  const collapsedBranches=new Set();let collapseScope='';
  const hasCommittedReceipt=leaf=>Boolean(string(leaf?.result_id).trim());
  let driftIndexForest=null,driftIndex=null;
  const getDriftIndex=()=>{
    const forest=snapshot.forest||{};if(driftIndexForest===forest&&driftIndex)return driftIndex;
    const roots=Array.isArray(forest.roots)?forest.roots:[],branches=Array.isArray(forest.branches)?forest.branches:[],leaves=Array.isArray(forest.leaves)?forest.leaves:[],allNodes=[...roots,...branches,...leaves];
    const nodesById=new Map(allNodes.filter(node=>node?.id).map(node=>[node.id,node])),leavesById=new Map(leaves.filter(node=>node?.id).map(node=>[node.id,node])),childrenByParent=new Map();
    allNodes.forEach(node=>{const parentId=string(node?.parent_id);if(!parentId)return;if(!childrenByParent.has(parentId))childrenByParent.set(parentId,[]);childrenByParent.get(parentId).push(node);});
    driftIndexForest=forest;driftIndex={nodesById,leavesById,childrenByParent};return driftIndex;
  };
  const driftedPrerequisites=leaf=>{
    const dependencies=Array.isArray(leaf?.unmet_dependencies)?leaf.unmet_dependencies:[];if(!dependencies.length)return [];
    const {nodesById,leavesById,childrenByParent}=getDriftIndex();
    const descendantLeaves=(nodeId,path=[],visited=new Set())=>{
      if(visited.has(nodeId))return [];
      visited.add(nodeId);
      const directLeaf=leavesById.get(nodeId);if(directLeaf)return [{leaf:directLeaf,path}];
      return (childrenByParent.get(nodeId)||[]).flatMap(child=>descendantLeaves(child.id,leavesById.has(child.id)?path:[...path,child],new Set(visited)));
    };
    const found=[],seen=new Set();
    dependencies.forEach(dependencyId=>{
      const dependency=nodesById.get(dependencyId);if(!dependency)return;
      const candidates=leavesById.has(dependencyId)?[{leaf:dependency,path:[]}]:descendantLeaves(dependencyId);
      candidates.forEach(({leaf:source,path})=>{
        if(source.state!=='delivered'||source.artifact_state!=='mismatch')return;
        const key=dependencyId+'\u0000'+source.id;if(seen.has(key))return;seen.add(key);found.push({dependency,leaf:source,path});
      });
    });
    return found;
  };
  const isCommittedDelivery=(previous,next,initial=false)=>!initial&&Boolean(previous)&&next?.state==='delivered'&&hasCommittedReceipt(next)&&(previous.state!=='delivered'||previous.result_id!==next.result_id);
  const transitionMotion=(previous,next,initial=false)=>initial?'':!previous?'arrived':previous===next?'':next==='running'?'started':next==='claimed'?'just-claimed':next==='delivered'?'completed':next==='blocked'?'just-blocked':'';
  const taskHandoffPrompt=(leaf,agent,workspace)=>[`请你作为 AgentBus 成员 ${agent} 接续当前项目任务。`,`项目控制目录：${workspace||'请先确认当前项目'}`,`任务 ID：${leaf.task_id||''}`,`任务标题：${leaf.title||''}`,`目标摘要：${leaf.goal||'请以任务原文为准。'}`,'请先阅读项目 AGENTS.md、当前任务书和该任务原文，核对结果收件人、输入、允许目录与验收条件。','只有确认任务仍在你自己的 ready 信箱时，才通过 AgentBus 正式入口认领并执行。','若任务不在本人 ready 信箱、已被他人认领或状态不明，不要接管或重做，不要覆盖或重复执行，只汇报当前状态。','完成后按任务中声明的 result_to 提交正规回执，并记录真实产物与未验证项。','这段文字只是接手提示；复制不代表消息已送达、任务已认领或 Agent 正在运行。'].join('\n');
  const presenceNote=presence=>presence.mode==='working'?`${presence.running} 个任务有执行心跳${presence.blocked?' · '+presence.blocked+' 项受阻':''}`:presence.mode==='blocked'?`${presence.blocked} 个任务受阻 · 查看断点`:presence.mode==='claimed'?`${presence.claimed} 个任务已认领${presence.blocked?' · '+presence.blocked+' 项受阻':''} · 等待心跳`:presence.mode==='completed'?'本轮分配任务已交付':presence.mode==='disabled'?'成员已停用':'当前没有执行心跳';
  const presenceFor=(member,leaves)=>{const owned=leaves.filter(leaf=>leaf.owner===member.id);const running=owned.filter(leaf=>leaf.state==='running').length;const claimed=owned.filter(leaf=>leaf.state==='claimed').length;const blocked=owned.filter(leaf=>leaf.state==='blocked'&&!leaf.unmet_dependencies?.length).length;return member.enabled===false?{mode:'disabled',running,claimed,blocked,owned:owned.length}:running?{mode:'working',running,claimed,blocked,owned:owned.length}:blocked?{mode:'blocked',running,claimed,blocked,owned:owned.length}:claimed?{mode:'claimed',running,claimed,blocked,owned:owned.length}:owned.length&&owned.every(leaf=>leaf.state==='delivered')?{mode:'completed',running,claimed,blocked,owned:owned.length}:{mode:'idle',running,claimed,blocked,owned:owned.length};};
  const workbenchFor=leaves=>{const running=leaves.filter(leaf=>leaf.state==='running').length,claimed=leaves.filter(leaf=>leaf.state==='claimed').length,blocked=leaves.filter(leaf=>leaf.state==='blocked'&&!leaf.unmet_dependencies?.length).length,delivered=leaves.filter(leaf=>leaf.state==='delivered').length,completed=leaves.length>0&&delivered===leaves.length;return{mode:running?'working':blocked?'blocked':claimed?'claimed':completed?'completed':'idle',running,claimed,blocked,delivered,completed};};
  const el = (tag, className, text) => {const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined)node.textContent=string(text); return node;};
  const append = (parent,...children) => {children.forEach(child=>parent.appendChild(child));return parent;};
  /**
   * 构造内联 SVG 图标元素字符串。
   * @param {string} name - 图标标识名
   * @param {string} [className='icon'] - 附加的 CSS 类名
   * @returns {string} SVG HTML 片段字符串
   */
  /**
   * 构造内联 SVG 图标元素字符串。
   * @param {string} name - 图标标识名
   * @param {string} [className='icon'] - 附加的 CSS 类名
   * @returns {string} SVG HTML 片段字符串
   */
  function icon(name, className='icon') {const svg=document.createElementNS(NS,'svg');svg.setAttribute('class',className);svg.setAttribute('aria-hidden','true');const use=document.createElementNS(NS,'use');use.setAttribute('href','#i-'+name);svg.appendChild(use);return svg;}
  const memberRecord=identity=>snapshot.members.find(member=>member.id===identity);
  function memberLogo(identity, member=memberRecord(identity)) {
    const holder=el('span','member-icon-frame');holder.setAttribute('aria-hidden','true');
    const name=string(member?.name||member?.display_name||member?.label||member?.id||identity);
    const source=member?.identity_icon_data;
    holder.title=name+' · AgentBus 离线成员标识';
    const fallback=()=>{const words=name.trim().split(/[^\p{L}\p{N}]+/u).filter(Boolean),initials=words.length>1?words.slice(0,2).map(word=>word[0]).join(''):Array.from(words[0]||'AI').slice(0,2).join('');return el('span','member-icon-fallback',initials.toLocaleUpperCase()||'AI');};
    holder.dataset.iconState='monogram';
    if(typeof source==='string'&&/^data:image\/(?:svg\+xml|png);base64,[A-Za-z0-9+/=]+$/.test(source)){
      const image=el('img','desktop-app-icon');image.setAttribute('src',source);image.alt='';image.draggable=false;
      holder.dataset.iconState='bundled';
      image.addEventListener('error',()=>{holder.dataset.iconState='monogram';holder.replaceChildren(fallback());},{once:true});holder.appendChild(image);
    }else holder.appendChild(fallback());
    return holder;
  }
  function memberActivity(member,presence) {
    const labels={working:'正在处理任务 · 执行心跳活跃',idle:'待命中',claimed:'任务已接手 · 等待执行心跳',blocked:'任务受阻 · 需要排查断点',completed:'本轮分配任务已交付'};
    const activity=el('span','member-activity '+presence.mode);
    activity.setAttribute('aria-label',memberName(member.id)+'：'+(labels[presence.mode]||'等待状态'));
    activity.title=memberName(member.id)+' · '+(labels[presence.mode]||'等待状态');
    activity.style.setProperty('--bot-phase',(-snapshot.members.indexOf(member)*.75)+'s');
    const bot=el('span','activity-bot'),antenna=el('span','bot-antenna'),face=el('span','bot-face');
    append(face,el('span','bot-eye left'),el('span','bot-eye right'));
    append(bot,antenna,face);
    activity.appendChild(bot);
    if(presence.mode==='working'){
      const bubble=el('span','activity-bubble');
      append(bubble,el('span','dot'),el('span','dot'),el('span','dot'));
      activity.appendChild(bubble);
    }
    else if(presence.mode==='claimed'){
      const badge=el('span','activity-badge claimed','已接手');
      activity.appendChild(badge);
    }
    else if(presence.mode==='blocked'){
      const badge=el('span','activity-badge alert','!');
      activity.appendChild(badge);
    }
    else if(presence.mode==='completed'){
      const badge=el('span','activity-badge complete','✓');
      activity.appendChild(badge);
    }
    return activity;
  }
  /**
   * 弹出浮层通知提示条（Toast），支持自动消失与点击动作。
   * @param {string} message - 提示消息文本
   * @param {boolean} [error=false] - 是否为错误/警告级别样式
   * @param {Object|null} [action=null] - 额外挂载的可点击操作配置
   * @returns {void}
   */
  /**
   * 弹出浮层通知提示条（Toast），支持自动消失与点击动作。
   * @param {string} message - 提示消息文本
   * @param {boolean} [error=false] - 是否为错误/警告级别样式
   * @param {Object|null} [action=null] - 额外挂载的可点击操作配置
   * @returns {void}
   */
  function toast(message,error=false,action=null) {clearTimeout(toastTimer);toastAction=typeof action==='function'?action:null;$('toast').textContent=string(message);$('toast').disabled=!toastAction;$('toast').classList.toggle('error',error);$('toast').classList.toggle('actionable',Boolean(toastAction));$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?7000:4200);}
  const bridge = () => window.pywebview && window.pywebview.api && typeof window.pywebview.api.call === 'function';
  /**
   * 更新前端总线连接状态指示灯与在线标志。
   * @param {boolean} live - 是否处于连通在线状态
   * @returns {void}
   */
  /**
   * 更新前端总线连接状态指示灯与在线标志。
   * @param {boolean} live - 是否处于连通在线状态
   * @returns {void}
   */
  function setConnection(live) {
    online=Boolean(live);const configured=snapshot.project.configured===true;
    if(!online&&autoDispatchActive)stopAutomaticDispatch();
    $('connectionLabel').textContent=!online?'离线预览 · 只读':configured?'桌面桥梁已连接':'已打开 · 先设置项目';
    $('connectionBadge').classList.toggle('connected',online&&configured);$('connectionBadge').querySelector('.status-dot').className='status-dot '+(online&&configured?'emerald':online?'amber':'neutral');
    $('previewNotice').hidden=online;$('stopButton').disabled=!online||!configured;
    updateFirstRunGuide();
    updateFlowControls();
  }
  function updateFirstRunGuide() {
    if(!ready)return;const empty=$('canvasEmpty'),setupId='firstRunSetup';let setup=$(setupId);
    if(snapshot.project.configured!==true){empty.hidden=false;empty.querySelector('h2').textContent='先设置项目与成员';empty.querySelector('p').textContent=online?'选择项目目录、成员与组长；随后导入已确定的项目任务树。':'打开 AgentBus 桌面应用后，先选择项目目录、成员与组长。';
      if(!setup){setup=el('button','button primary');setup.id=setupId;setup.type='button';setup.addEventListener('click',showSettings);empty.appendChild(setup);}setup.textContent=online?'设置项目与成员':'等待桌面设置';setup.hidden=!online;setup.disabled=!online;return;}
    if(setup)setup.hidden=true;
    let treeSetup=$('firstTreeSetup');
    if(!snapshot.forest.roots.length&&!snapshot.forest.leaves.length){empty.hidden=false;empty.querySelector('h2').textContent='导入项目任务树';empty.querySelector('p').textContent='项目目标与任务范围已由组长确定。导入现有任务树，或从可复用模板开始。';if(!treeSetup){treeSetup=el('button','button primary','导入任务树');treeSetup.id='firstTreeSetup';treeSetup.type='button';treeSetup.addEventListener('click',showTreeBuilder);empty.appendChild(treeSetup);}treeSetup.hidden=!online;treeSetup.disabled=!online;}
    else if(treeSetup)treeSetup.hidden=true;
  }
  // =========================================================================
  // §7.5 前端 call() 超时熔断机制 (AUDIT-FRONTEND-TIMEOUT)
  // =========================================================================
  const DEFAULT_CALL_TIMEOUT_MS = 30000;
  let lastCallTimeout = false;

  /**
   * 向 WebBridge 发起异步 RPC 操作请求并处理应答与异常。
   * @param {string} action - 后端 RPC 方法名
   * @param {Object} [payload={}] - 请求参数对象
   * @param {Object} [options={}] - 请求选项（静默、重试等）
   * @returns {Promise<Object>} 后端响应数据对象
   */
  /**
   * 向 WebBridge 发起异步 RPC 操作请求并处理应答与异常。
   * @param {string} action - 后端 RPC 方法名
   * @param {Object} [payload={}] - 请求参数对象
   * @param {Object} [options={}] - 请求选项（静默、重试等）
   * @returns {Promise<Object>} 后端响应数据对象
   */
  async function call(action, payload={}, options={}) {
    if(!bridge()) throw new Error('当前是离线只读预览。请在 AgentBus 桌面工作台中执行此操作。');
    if (isMutatingAction(action, payload)) mutationGeneration++;
    const quiet = options?.quiet === true;
    if (!quiet) setConnection(true);

    const timeoutMs = Number(options?.timeoutMs || payload?._timeoutMs) || (action === 'export_tree' || action === 'import_tree' ? 60000 : DEFAULT_CALL_TIMEOUT_MS);

    let timer = null;
    const timeoutPromise = new Promise((_, reject) => {
      timer = setTimeout(() => {
        if (!quiet) lastCallTimeout = true;
        const err = new Error(`桌面接口调用超时（超过 ${Math.round(timeoutMs / 1000)} 秒无响应）。可能后台正在繁忙处理或进程阻塞，请稍后重试。`);
        err.code = 'TIMEOUT';
        err.action = action;
        reject(err);
      }, timeoutMs);
    });

    try {
      const response = await Promise.race([
        window.pywebview.api.call(action, payload),
        timeoutPromise
      ]);
      clearTimeout(timer);
      if (!quiet) lastCallTimeout = false;
      if(!response || response.ok!==true) throw new Error(string(response && response.error) || '桌面接口没有返回有效回执。');
      return response.data;
    } catch (error) {
      clearTimeout(timer);
      if (error && error.code === 'TIMEOUT') {
        if (!quiet) {
          refreshInFlight = false;
          $('refreshButton')?.classList.remove('processing');
          toast(error.message, true);
        }
      }
      throw error;
    }
  }
  async function persistTopology(payload) {
    const generation = mutationGeneration + 1;
    await call('update_leaf_topology', payload);
    // Topology replies contain the forest only, not the full workbench. Read
    // the authoritative derived state explicitly even while an editor is open.
    try {
      setSnapshot(await call('snapshot', {}), generation);
    } catch (error) {
      throw new Error('修改已提交，但最新状态读取失败。请刷新确认：' + error.message);
    }
  }
  function configurationForMode(mode) {
    const config=snapshot.config,project=snapshot.project;
    return {workspace:config.workspace||project.workspace,output_root:config.output_root||project.output_root||'.',coordinator:config.director||project.coordinator,assignment_mode:mode,members:snapshot.members.filter(member=>!member.observed).map(member=>({id:member.id,display_name:member.name||member.display_name||member.label,enabled:member.enabled!==false,communication_mode:member.communication_mode||member.transport||'file'}))};
  }
  async function saveAssignmentMode(mode) {
    if(!['automatic','manual','autonomous'].includes(mode))throw new Error('请选择一种任务指派方式。');
    if(!online||snapshot.project.configured!==true)throw new Error('请先保存项目与成员设置。');
    stopAutomaticDispatch();assignmentSaving=true;updateFlowControls();
    try{const data=await call('configure',configurationForMode(mode));if(data?.forest)setSnapshot(data);else await refresh();if(data?.workflow_ready===false){toast('调度模式已保存，但接入工作流尚未准备：'+(data.workflow_error||'请检查原文件并重新保存。'),true);}else{toast(mode==='manual'?'已切换为架构师定向委派。选定任务卡即可定向委派责任人。':mode==='autonomous'?'已切换为自由调度模式。各 Agent 可自主运行 claim-next 认领接活。':'已切换为自适应流水线调度。点击“启动流水线流转”即可按依赖自动流转。');}return data;}
    finally{assignmentSaving=false;updateFlowControls();}
  }
  function updateFlowControls() {
    const configured=online&&snapshot.project.configured===true,mode=assignmentMode(),select=$('assignmentMode'),button=$('autoDispatchButton');
    if(!select||!button)return;
    select.value=mode;select.disabled=!configured||assignmentSaving;
    $('importTreeButton').disabled=!configured;$('exportTreeButton').disabled=!configured||!snapshot.forest.roots.length;
    $('exportTreeButton').title=!snapshot.forest.roots.length?'请先创建或导入任务树，再导出模板。':!configured?'请先保存项目与成员设置。':'导出结构模板，可在其他项目重新配置成员使用。';
    if($('exportBackupButton'))$('exportBackupButton').disabled=!configured||!snapshot.forest.roots.length;
    if($('restoreBackupButton'))$('restoreBackupButton').disabled=!configured;
    button.hidden=mode!=='automatic';button.disabled=!canAutoDispatch(online,snapshot.project,snapshot.forest,mode)||assignmentSaving;
    button.textContent=autoDispatchActive?'停止自动指派':'开始自动指派';button.setAttribute('aria-pressed',String(autoDispatchActive));
    const quickDispatchTxt=$('quickDispatchText');
    if(quickDispatchTxt){
      quickDispatchTxt.textContent=snapshot.forest.stopped?'已安全熔断':!autoDispatchActive?'启动调度':document.hidden?'后台挂起中':'停止调度';
    }
    const quickModeTxt=$('quickModeSwitchText');
    if(quickModeTxt){
      quickModeTxt.textContent=mode==='autonomous'?'⚡ 自由抢单':mode==='manual'?'✋ 手动指定':'🤖 自动派发';
    }
    $('assignmentNote').textContent=snapshot.forest.stopped?'⚠️ 全局安全熔断生效中 · 任务投递冻结':mode==='manual'?'架构师定向委派：选定任务节点生成接入指令':mode==='autonomous'?'自由调度模式：Agent 自主接活抢单（摆脱组长单点瓶颈）':autoDispatchActive?(document.hidden?'窗口在后台/最小化 · 自动派工已暂停（仅前台激活）':'自适应流水线运行中 · 实时投递就绪任务'):'自适应流水线已挂起 · 就绪任务等待流转';
    if($('checkModeAuto'))$('checkModeAuto').textContent=mode==='automatic'?'✓':'';
    if($('checkModeAutonomous'))$('checkModeAutonomous').textContent=mode==='autonomous'?'✓':'';
    if($('checkModeManual'))$('checkModeManual').textContent=mode==='manual'?'✓':'';
    renderProjectFlow();
  }
  function stopAutomaticDispatch() {autoDispatchActive=false;autoDispatchGeneration++;clearTimeout(autoDispatchTimer);autoDispatchTimer=null;updateFlowControls();}
  function scheduleAutomaticDispatch() {
    clearTimeout(autoDispatchTimer);autoDispatchTimer=null;
    if(!autoDispatchActive||document.hidden||!canAutoDispatch(online,snapshot.project,snapshot.forest,assignmentMode()))return;
    autoDispatchTimer=setTimeout(runAutomaticDispatch,autoDispatchDelay);
  }
  /**
   * 执行自动派发循环：检索就绪队列并调度空闲合格成员进行认领。
   * @returns {Promise<void>}
   */
  /**
   * 执行自动派发循环：检索就绪队列并调度空闲合格成员进行认领。
   * @returns {Promise<void>}
   */
  async function runAutomaticDispatch() {
    if(!autoDispatchActive||autoDispatchInFlight||document.hidden||!canAutoDispatch(online,snapshot.project,snapshot.forest,assignmentMode()))return;
    const generation=autoDispatchGeneration;autoDispatchInFlight=true;
    try{const data=await call('dispatch_ready',{});if(generation!==autoDispatchGeneration)return;if(data?.forest)setSnapshot(data);else await refresh();const summary=data?.dispatch_summary||{};
      autoDispatchDelay=Number(summary.assigned)>0?20000:Math.min(60000,autoDispatchDelay+10000);
      if(Number(summary.assigned)>0)toast('已分配 '+summary.assigned+' 项就绪任务至成员信箱（纯文件事件，不自动调用外部模型）。');
    }catch(error){if(generation===autoDispatchGeneration){stopAutomaticDispatch();toast('自动指派已暂停：'+error.message,true);}}
    finally{autoDispatchInFlight=false;if(generation===autoDispatchGeneration)scheduleAutomaticDispatch();updateFlowControls();}
  }
  function startAutomaticDispatch() {
    if(!canAutoDispatch(online,snapshot.project,snapshot.forest,assignmentMode()))return toast('请先保存项目，选择自动指派，并恢复派工。',true);
    if(autoDispatchInFlight)return toast('上一轮指派正在完成，请稍后再开始。');
    autoDispatchActive=true;autoDispatchDelay=20000;autoDispatchGeneration++;updateFlowControls();runAutomaticDispatch();
  }
  function renderProjectFlow() {
    const target=$('flowSteps');if(!target)return;target.replaceChildren();const configured=snapshot.project.configured===true,hasTree=snapshot.forest.roots.length>0;
    const steps=[
      {title:'组织架构与协调者',note:configured?memberName(snapshot.config.director||snapshot.project.coordinator)+' 主持编排 · 目录已绑定':'定义团队成员、协调者与双目录拓扑',done:configured,action:showSettings},
      {title:'构建任务拓扑网',note:hasTree?snapshot.forest.leaves.length+' 个契约节点就绪':'导入已确定的任务树，或从可复用模板开始',done:hasTree,action:showTreeBuilder},
      {title:'选择任务分配方式',note:assignmentMode()==='manual'?'自己选择执行成员':'由组长自动安排',done:configured,action:showAssignmentChoice},
      {title:'智能体协议接入',note:'下发基线工作流协议，建立独立信箱会话',done:false,action:showInitialization},
      {title:'启动协同流转',note:assignmentMode()==='manual'?'选定拓扑节点，定向委派责任人':'开启自适应流水线，就绪任务自动投递',done:false,action:()=>{if(assignmentMode()==='automatic')startAutomaticDispatch();else{closeDialogIfOpen();$('leafSearch').focus();toast('搜索任务序号或点击任务卡，即可定向委派执行成员。');}}},
      {title:'过程审计与交接',note:'实时心跳监控、产物验真与安全交接',done:false,action:()=>showView('recovery')}
    ];
    steps.forEach((step,index)=>{const button=el('button','flow-step'+(step.done?' complete':''));button.type='button';button.disabled=!online||(index>0&&!configured);append(button,el('span','flow-step-number',String(index+1).padStart(2,'0')),append(el('span','flow-step-copy'),el('strong','',step.title),el('small','',step.note)));button.addEventListener('click',step.action);target.appendChild(button);});
    $('flowSummary').textContent=!configured?'先配置成员与协调者':!hasTree?'下一步：构建或导入任务拓扑网':autoDispatchActive?(document.hidden?'窗口在后台 · 自动派工已挂起（切回前台后恢复）':'自适应调度已开启 · 实时流转就绪任务'):'点击任务节点，即可定向委派并生成执行指令';
    if(!ready)$('projectFlow').open=true;
  }
  function closeDialogIfOpen(){if($('workspaceDialog').open)closeDialog();}
  function showTreeBuilder(){const content=openDialog('项目任务树','协同流程 · 02');append(content,el('p','panel-intro','任务树承载本项目已经确定的范围。导入项目任务树后即可开始协作；模板可在下一步选择。'));
    const choose=el('button','button primary','选择任务树');choose.disabled=!online;choose.addEventListener('click',showTreeImporter);append(content,append(el('div','dialog-action-row'),choose));}
  function showAssignmentChoice(){const content=openDialog('选择任务分配方式','协同流程 · 03');append(content,el('p','panel-intro','自动安排：组长按依赖与负载分配就绪任务；自由调度：各 Agent 自主认领任务，摆脱单点瓶颈；我来选择：手动挑选成员并生成接手指令。切换方式不撤销已发出的任务。'));
    const modeConfig=[
      {mode:'automatic',title:'自动安排（组长派发）',desc:'满足前序条件的任务由组长依次流转分配。'},
      {mode:'autonomous',title:'自由调度（Agent自动接活）',desc:'Agent 无需等待组长派单，运行 claim-next 按关键路径与依赖自主认领任务。'},
      {mode:'manual',title:'我来选择（手动指定）',desc:'打开任务卡后手动指定执行成员，再复制生成的接手指令。'}
    ];
    modeConfig.forEach(item=>{const button=el('button','assignment-choice'+(assignmentMode()===item.mode?' selected':''));button.type='button';append(button,el('strong','',item.title),el('span','',item.desc));button.disabled=!online;button.addEventListener('click',async()=>{button.disabled=true;try{await saveAssignmentMode(item.mode);closeDialog();}catch(error){toast(error.message,true);button.disabled=!online;}});content.appendChild(button);});}
  function showInitialization(){const content=openDialog('让成员接入当前项目','项目流程 · 04');append(content,el('p','panel-intro','先在对应软件打开当前项目的新对话，再发送一次初始接入指令。之后每个成员只需处理分配给自己的任务。复制指令不等于已连接或正在工作。'));
    const members=snapshot.members.filter(member=>member.enabled&&!member.observed);if(!members.length)content.appendChild(el('p','panel-empty','请先配置并保存项目成员。'));
    members.forEach(member=>{const row=el('div','initialization-row'),mark=el('span','initialization-mark');mark.appendChild(memberLogo(member.id));append(row,mark,append(el('div','initialization-copy'),el('strong','',member.name),el('span','',routeName(member.communication_mode||member.transport))));const entry=el('button','button small','生成初始指令');entry.type='button';entry.disabled=!online;entry.addEventListener('click',()=>showMemberPrompt(member.id));row.appendChild(entry);content.appendChild(row);});}
  async function showTreeImporter(){const content=openDialog('导入可复用任务树','项目流程 · 02'),version=dialogVersion;append(content,el('p','panel-intro','导入只添加结构、目标和前序关系。原项目的执行人、心跳和完成状态不会带入，也不会自动开始任务。'));
    const file=el('button','button primary','选择任务树文件');file.type='button';file.disabled=!online;file.addEventListener('click',()=>previewTreeImport({}));content.appendChild(file);
    const list=el('div','template-list'),loading=el('p','loading-copy','正在读取可用模板…');content.appendChild(list);list.appendChild(loading);
    try{const data=await call('tree_templates',{});if(version!==dialogVersion)return;list.replaceChildren();(data.templates||[]).forEach(template=>{const card=el('button','template-card');card.type='button';append(card,el('strong','',template.title),el('span','',template.description||'预览模板中的任务与依赖'));card.addEventListener('click',()=>previewTreeImport({template_id:template.id}));list.appendChild(card);});if(!list.childNodes.length)list.appendChild(el('p','panel-empty','没有内置模板。可以选择导出的任务树文件。'));}
    catch(error){if(version===dialogVersion){loading.remove();list.appendChild(el('p','error-note',error.message));}}
  }
  async function previewTreeImport(request) {
    const content=openDialog('预览导入内容','任务树模板'),version=dialogVersion,loading=el('p','loading-copy','正在验证任务树文件…');content.appendChild(loading);
    try{const data=await call('import_tree',{...request,confirm:false});if(version!==dialogVersion)return;if(data?.cancelled){showTreeImporter();return;}loading.remove();renderImportPreview(content,data,request);}
    catch(error){if(version===dialogVersion){loading.remove();content.appendChild(el('p','error-note',error.message));}}
  }
  function renderImportPreview(content,data,request) {
    const preview=data.preview||{},selected=data.template_id?{template_id:data.template_id}:request.template_id?{template_id:request.template_id}:data.path?{path:data.path}:null;
    if(selected&&typeof data.preview_id==='string'&&data.preview_id)selected.preview_id=data.preview_id;
    append(content,el('h3','template-title',preview.title||'任务树模板'),el('p','panel-intro',`${preview.branches??0} 个工作分支 · ${preview.leaf_count??preview.leaves??0} 项任务。默认追加，保留当前任务与成果。`));
    const impact=data.impact||{},mode=el('select','field-select');
    mode.setAttribute('aria-label','导入方式');
    const addOption=el('option','','追加到当前任务树'),replaceOption=el('option','','替换当前任务树 · 归档旧待办');
    addOption.value='append';replaceOption.value='replace';replaceOption.disabled=impact.can_replace===false;
    append(mode,addOption,replaceOption);mode.value='append';content.appendChild(mode);
    content.appendChild(el('p','panel-intro',impact.notice||'替换会归档旧树，产物和已完成回执保留。'));
    const impacted=[...(impact.processing||[]),...(impact.pending||[])];
    if(impacted.length){const list=el('ul','import-impact');impacted.forEach(row=>list.appendChild(el('li','',`${row.agent} · ${row.folder==='processing'?'正在接手':'未完成待办'} · ${row.title||row.task_id}`)));content.appendChild(list);}
    if(impact.can_replace===false)content.appendChild(el('p','import-warning','仍有成员持有在途任务，暂不能替换；可追加，或等任务完成后重新预览。'));
    (preview.warnings||[]).forEach(warning=>content.appendChild(el('p','import-warning',warning)));
    const outline=Array.isArray(data.outline?.items)?data.outline.items:[];
    if(outline.length){const section=el('section','import-outline');section.appendChild(el('h3','','将载入的目标与任务'));outline.forEach(item=>{const row=el('article','import-outline-row level-'+item.level);append(row,el('strong','',item.kind+' · '+item.title));if(item.goal)row.appendChild(el('p','','目标：'+item.goal));if(item.dependencies?.length)row.appendChild(el('small','','前置任务：'+item.dependencies.join('、')+(item.dependencies_truncated?`，另有 ${item.dependencies_truncated} 项`:'')));if(item.artifact_count)row.appendChild(el('small','','预期产物：'+(item.expected_artifacts||[]).join('、')+(item.artifacts_truncated?`，另有 ${item.artifacts_truncated} 项`:'')));section.appendChild(row);});if(data.outline.truncated)section.appendChild(el('p','import-warning',`预览最多展开 120 项，另有 ${data.outline.truncated} 项已验证，确认后会一并导入。`));content.appendChild(section);}
    const actions=el('div','dialog-action-row'),confirm=el('button','button primary','确认追加任务树'),back=el('button','button','返回选择');confirm.type=back.type='button';confirm.disabled=!online||!selected;
    mode.addEventListener('change',()=>{confirm.textContent=mode.value==='replace'?'归档旧待办并替换任务树':'确认追加任务树';});
    if(!selected||!selected.preview_id)content.appendChild(el('p','error-note','这份内容没有有效预览凭据，请重新选择并预览后再导入。'));
    confirm.addEventListener('click',async()=>{confirm.disabled=true;try{const result=await call('import_tree',{...selected,confirm:true,replace:mode.value==='replace'});if(result?.cancelled){confirm.disabled=!online;return;}if(result?.forest){setSnapshot(result);void ensureProjectGuide(result.project);}else await refresh();closeDialog();toast(result.recovery_path?'任务树已替换，旧树归档在 '+result.recovery_path:'任务已追加，原有工作保持不变。');}catch(error){toast(error.message,true);confirm.disabled=!online;}});back.addEventListener('click',showTreeImporter);append(actions,back,confirm);content.appendChild(actions);
  }
  function playCrispDeliveryTone() {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      const ctx = new AudioContext();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      // 轻脆双阶和音 (高频轻快叮咚声: 880Hz -> 1320Hz)
      const now = ctx.currentTime;
      osc.frequency.setValueAtTime(880, now);
      osc.frequency.exponentialRampToValueAtTime(1320, now + 0.08);
      gain.gain.setValueAtTime(0.15, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.35);
    } catch (_) {}
  }

  function triggerDeliveryNotice(leaf) {
    playCrispDeliveryTone();
    const title = '任务正式交付完成';
    const message = `[§${taskNumber(leaf) || leaf.id}] ${leaf.title || '任务'} 已核验回执并交付！`;
    if (bridge()) {
      call('show_desktop_notification', {
        title,
        message,
        sound: true,
        task_id: leaf.task_id || leaf.id
      }).catch(() => {});
    }
  }

  /**
   * 将任务森林无限画布导出为高清矢量 SVG 或光栅 PNG 图片。
   * @param {'png'|'svg'} [format='png'] - 导出目标格式
   * @returns {Promise<void>}
   */
  /**
   * 将任务森林无限画布导出为高清矢量 SVG 或光栅 PNG 图片。
   * @param {'png'|'svg'} [format='png'] - 导出目标格式
   * @returns {Promise<void>}
   */
  function captureCanvasExport() {
    const folds = [...collapsedBranches], previousFocus = document.activeElement;
    const focusedBranch = previousFocus?.closest?.('.branch-node')?.dataset.group;
    try {
      // Export the complete tree, while keeping the user's folded view intact.
      if (folds.length) { collapsedBranches.clear(); renderForest(true); }
      const dark = document.body.dataset.theme === 'dark';
      const nodeById = new Map([...snapshot.forest.roots, ...snapshot.forest.branches, ...snapshot.forest.leaves].map(node => [node.id, node]));
      const nodes = [...document.querySelectorAll('.forest-node')].map(node => {
        const id = node.dataset.leafId || node.dataset.group || node.dataset.rootId;
        const data = nodeById.get(id), leaf = node.classList.contains('leaf-card');
        const title = data ? (leaf ? numberedTitle(data) : [data.section_no, data.title].filter(Boolean).join(' · ')) : node.title || node.textContent;
        const x = Number(node.dataset.x) || 0, y = Number(node.dataset.y) || 0;
        const width = Number(node.dataset.width) || node.offsetWidth || 200;
        const height = Number(node.dataset.height) || node.offsetHeight || 90;
        let fill = dark ? '#1e293b' : '#ffffff', stroke = dark ? '#334155' : '#cbd5e1';
        if (node.classList.contains('trunk-node')) stroke = '#3b82f6';
        else if (node.classList.contains('branch-node')) stroke = '#8b5cf6';
        else if (leaf) {
          stroke = ({delivered:'#10b981',running:'#0284c7',claimed:'#f59e0b',blocked:'#ef4444'})[data?.state] || stroke;
        }
        const lines = []; let line = '', used = 0;
        for (const char of Array.from(string(title))) {
          const charWidth = char.codePointAt(0) > 255 ? 13 : 8;
          if (char === '\n' || (line && used + charWidth > width - 24)) { lines.push(line); line = ''; used = 0; }
          if (char !== '\n') { line += char; used += charWidth; }
        }
        if (line || !lines.length) lines.push(line);
        const caption = leaf ? `${taskStateLabel(data)} · ${memberName(data?.owner)}` : node.querySelector('.trunk-meta, .branch-meta')?.textContent || '';
        return {id, x, y, width, height, fill, stroke, title, lines, caption};
      });
      if (!nodes.length) throw new Error('当前没有可导出的任务节点');
      // Long titles get room in both formats; stretch vertical geometry and
      // connectors together instead of clipping text or overlapping rows.
      const stretch = Math.max(1, ...nodes.map(node => (28 + node.lines.length * 18 + (node.caption ? 24 : 0)) / node.height));
      nodes.forEach(node => { node.y *= stretch; node.height *= stretch; });
      const paths = [...document.querySelectorAll('.forest-lines path:not(.edge-flow)')].map(path => ({
        d: path.getAttribute('d') || '',
        stroke: getComputedStyle(path).stroke || (dark ? '#64748b' : '#94a3b8'),
        dash: path.classList.contains('dep-wire') ? [6, 5] : []
      })).filter(path => path.d);
      const minX = Math.min(...nodes.map(node => node.x)) - 60, minY = Math.min(...nodes.map(node => node.y)) - 60;
      const width = Math.max(...nodes.map(node => node.x + node.width)) - minX + 60;
      const height = Math.max(...nodes.map(node => node.y + node.height)) - minY + 120;
      const renderScale = Math.max(1, Math.min(4, 3840 / Math.max(width, height)));
      return {nodes, paths, stretch, minX, minY, width, height, renderScale, dark,
        targetW: Math.ceil(width * renderScale), targetH: Math.ceil(height * renderScale),
        footer: `AgentBus · ${projectName()} · 共 ${snapshot.forest.leaves.length} 项任务 · ${$('capsuleText')?.textContent || ''}`};
    } finally {
      if (folds.length) { folds.forEach(id => collapsedBranches.add(id)); renderForest(true); }
      if (previousFocus?.dataset?.leafId) [...document.querySelectorAll('.leaf-card')].find(node => node.dataset.leafId === previousFocus.dataset.leafId)?.focus();
      else if (focusedBranch) branchElements(focusedBranch)?.querySelector('.branch-collapse-button')?.focus();
    }
  }

  function showCanvasExport() {
    if (!snapshot.forest.roots.length) {toast('请先创建或导入任务树，再导出画布大图。', true);return;}
    const content = openDialog('导出完整任务树', '文件 · PNG / SVG');
    content.appendChild(el('p', 'panel-intro', '导出包含全部节点、完整标题与连线，保留当前折叠视图。PNG 按任务树尺寸生成长图；SVG 可持续放大，适合很大的任务树。'));
    const actions = el('div', 'dialog-action-row');
    ['png', 'svg'].forEach(format => {
      const button = el('button', 'button' + (format === 'png' ? ' primary' : ''), '导出 ' + format.toUpperCase());
      button.type = 'button';button.addEventListener('click', () => {closeDialog();void exportCanvasImage(format);});actions.appendChild(button);
    });
    content.appendChild(actions);
  }

  async function exportCanvasImage(format = 'png') {
    if (!snapshot.forest.roots.length) { toast('请先创建或导入任务树，再导出画布大图。', true); return; }
    toast('正在生成完整任务树大图，请稍候…');
    try {
      const scene = captureCanvasExport(), {nodes, paths, stretch, minX, minY, width, height, renderScale, targetW, targetH, dark} = scene;
      const textColor = dark ? '#f8fafc' : '#0f172a', background = dark ? '#171a21' : '#f8fafc';
      const xml = value => string(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'})[char]);
      let dataUrl, b64;
      if (format === 'svg') {
        const markup = [`<svg xmlns="http://www.w3.org/2000/svg" width="${targetW}" height="${targetH}" viewBox="${minX} ${minY} ${width} ${height}">`,
          `<rect x="${minX}" y="${minY}" width="${width}" height="${height}" fill="${background}"/>`,
          `<g transform="scale(1 ${stretch})" fill="none" stroke-width="2">`,
          ...paths.map(path => `<path d="${xml(path.d)}" stroke="${xml(path.stroke)}"${path.dash.length ? ` stroke-dasharray="${path.dash.join(' ')}"` : ''}/>`), '</g>',
          ...nodes.map(node => `<g data-node-id="${xml(node.id)}"><title>${xml(node.title)}</title><rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.height}" rx="8" fill="${node.fill}" stroke="${node.stroke}" stroke-width="2"/><text fill="${textColor}" font-family="sans-serif" font-size="13" font-weight="600">${node.lines.map((line, i) => `<tspan x="${node.x + 12}" y="${node.y + 24 + i * 18}">${xml(line)}</tspan>`).join('')}</text>${node.caption ? `<text x="${node.x + 12}" y="${node.y + node.height - 12}" fill="${textColor}" font-family="sans-serif" font-size="11" textLength="${node.width - 24}" lengthAdjust="spacingAndGlyphs">${xml(node.caption)}</text>` : ''}</g>`),
          `<text x="${minX + 20}" y="${minY + height - 25}" fill="${textColor}" font-family="sans-serif" font-size="13">${xml(scene.footer)}</text>`, '</svg>'];
        b64 = btoa(unescape(encodeURIComponent(markup.join('\n'))));
        dataUrl = 'data:image/svg+xml;base64,' + b64;
      } else {
        // Keep labels at their natural readable size. Oversize raster scenes
        // get an explicit SVG fallback instead of silently squeezed text.
        if (targetW > 32767 || targetH > 32767 || targetW * targetH > 80000000) throw new Error('任务树超出可读 PNG 画布上限，请使用 SVG 矢量导出');
        const canvas = document.createElement('canvas'); canvas.width = targetW; canvas.height = targetH;
        const ctx = canvas.getContext('2d'); if (!ctx) throw new Error('无法初始化导出画布');
        ctx.fillStyle = background; ctx.fillRect(0, 0, targetW, targetH);
        ctx.save(); ctx.translate(-minX * renderScale, -minY * renderScale); ctx.scale(renderScale, renderScale);
        ctx.save(); ctx.scale(1, stretch);
        paths.forEach(path => { ctx.strokeStyle = path.stroke; ctx.lineWidth = 2; ctx.setLineDash(path.dash); ctx.stroke(new Path2D(path.d)); });
        ctx.restore(); ctx.setLineDash([]);
        nodes.forEach(node => {
          ctx.fillStyle = node.fill; ctx.strokeStyle = node.stroke; ctx.lineWidth = 2; ctx.beginPath();
          if (ctx.roundRect) ctx.roundRect(node.x, node.y, node.width, node.height, 8); else ctx.rect(node.x, node.y, node.width, node.height);
          ctx.fill(); ctx.stroke(); ctx.fillStyle = textColor; ctx.font = '600 13px sans-serif';
          node.lines.forEach((line, i) => ctx.fillText(line, node.x + 12, node.y + 24 + i * 18));
          if (node.caption) {ctx.font = '11px sans-serif';ctx.fillText(node.caption, node.x + 12, node.y + node.height - 12, node.width - 24);}
        });
        ctx.font = '13px sans-serif'; ctx.fillStyle = textColor;
        ctx.fillText(scene.footer, minX + 20, minY + height - 25, width - 40); ctx.restore();
        dataUrl = canvas.toDataURL('image/png'); b64 = dataUrl.split(',')[1];
      }
      if (bridge()) {
        const result = await call('export_canvas_image', {format, data_base64:b64, width:targetW, height:targetH, save:true});
        if (!result?.cancelled) toast(`${format.toUpperCase()} 完整任务树已导出至: ${result.path || '指定位置'}`);
      } else {
        const link = document.createElement('a'); link.href = dataUrl; link.download = `task_forest_${projectName()}.${format}`; link.click();
        toast(`${format.toUpperCase()} 完整任务树已开始下载`);
      }
    } catch (error) { toast(`导出大图失败: ${error.message}`, true); }
  }


  async function exportTreeTemplate() {
    if(!snapshot.forest.roots.length){toast('请先创建或导入任务树，再导出树模板。',true);return;}
    const button=$('exportTreeButton');if(button&&button.disabled)return;if(button)button.disabled=true;
    try{const data=await call('export_tree',{});if(!data?.cancelled){const warnings=data?.warnings||data?.preview?.warnings||[];toast(warnings.length?'树模板已导出，但存在与当前工程不一致的警告，请查看说明。':'已导出当前任务树模板。执行状态与认领回执不会被导出。');}}
    catch(error){toast(exportError(error.message),true);}finally{updateFlowControls();}
  }

  async function exportHandover() {
    if(!snapshot.forest.roots.length){toast('请先创建或导入任务树，再导出交接总结。',true);return;}
    const button=$('exportHandoverButton');if(button)button.disabled=true;
    try{const data=await call('export_handover',{});if(!data?.markdown)throw new Error('未能生成交接总结。');showHandoverDialog(data);}
    catch(error){toast(error.message,true);}
    finally{if(button)button.disabled=!online;}
  }
  /**
   * 渲染并弹出成员交接（Handoff）看板与接手指令对话框。
   * @param {Object} data - 交接看板数据结构（任务、成员状态、断点说明）
   * @returns {void}
   */
  /**
   * 渲染并弹出成员交接（Handoff）看板与接手指令对话框。
   * @param {Object} data - 交接看板数据结构（任务、成员状态、断点说明）
   * @returns {void}
   */
  function showHandoverDialog(data) {
    const content=openDialog('导出项目交接总结','文件 · Markdown 归档'),stamp=String(data.sha256||'');
    append(content,el('p','panel-intro',`已按当前任务树生成结构化交接归档：共 ${data.bytes||0} 字节，SHA256 ${stamp.slice(0,16)}…。内容来自正式记录，导出本身不会改动任何任务、认领或回执；复制与保存都需要你自己确认。`));
    const box=el('textarea','handover-output');box.readOnly=true;box.value=data.markdown||'';box.rows=16;box.setAttribute('aria-label','项目交接总结 Markdown 正文');content.appendChild(box);
    const copy=el('button','button primary','复制归档正文'),save=el('button','button','保存为文件…');copy.type=save.type='button';
    copy.addEventListener('click',()=>copyText(box.value,'交接归档已复制到剪贴板。'));
    save.addEventListener('click',async()=>{save.disabled=true;try{const result=await call('export_handover',{save:true});if(result?.cancelled){save.disabled=false;return;}toast('已保存交接归档：'+(result?.path||result?.filename||'所选位置'));}
      catch(error){toast(error.message,true);}finally{save.disabled=!online;}});
    append(content,append(el('div','dialog-action-row'),copy,save));
  }
  /**
   * 触发导出全量工作区备份归档包。
   * @returns {Promise<void>}
   */
  /**
   * 触发导出全量工作区备份归档包。
   * @returns {Promise<void>}
   */
  async function exportBackup() {
    if(!snapshot.forest.roots.length){toast('请先创建或导入任务树，再导出项目备份。',true);return;}
    const content=openDialog('导出项目全局备份','轻量可移植备份 · 预览'),version=dialogVersion,loading=el('p','loading-copy','正在生成备份清单预览…');
    content.appendChild(loading);
    try{
      const preview=await call('export_backup',{preview_only:true});
      if(version!==dialogVersion)return;
      loading.remove();
      append(content,el('p','panel-intro','已排除凭据、模型私有会话和本机绝对路径；迁移至其他电脑后将自动绑定新工作区路径。'));
      const list=el('div','template-list');content.appendChild(list);
      (preview.manifest||[]).forEach(item=>{
        const card=el('div','template-card');
        append(card,el('strong','',item.item+` (${item.count})`),el('span','',item.detail));
        list.appendChild(card);
      });
      const actions=el('div','dialog-action-row'),save=el('button','button primary','保存备份文件 (.json)…');
      save.type='button';save.disabled=!online;
      save.addEventListener('click',async()=>{
        save.disabled=true;
        try{
          const res=await call('export_backup',{preview_only:false});
          if(res?.cancelled){save.disabled=!online;return;}
          closeDialog();
          toast('项目全局备份已成功导出至：'+(res.path||'所选文件'));
        }catch(e){toast(e.message,true);save.disabled=!online;}
      });
      actions.appendChild(save);content.appendChild(actions);
    }catch(error){
      if(version===dialogVersion){loading.remove();content.appendChild(el('p','error-note',error.message));}
    }
  }
  async function restoreBackup() {
    const content=openDialog('还原项目全局备份','轻量可移植备份 · 差异对比'),version=dialogVersion,loading=el('p','loading-copy','请在系统弹窗中选择备份文件…');
    content.appendChild(loading);
    try{
      const res=await call('restore_backup',{confirm:false});
      if(version!==dialogVersion)return;
      if(res?.cancelled){closeDialog();return;}
      loading.remove();
      renderRestorePreview(content,res);
    }catch(error){
      if(version===dialogVersion){loading.remove();content.appendChild(el('p','error-note',error.message));}
    }
  }
  function renderRestorePreview(content,res) {
    const preview=res.preview||{},diff=preview.diff_summary||{},tree=preview.tree_preview||{};
    append(content,el('p','panel-intro',`备份已校验 (SHA256: ${String(preview.bundle_sha256||'').slice(0,12)}…)。还原将更新当前任务树、团队配置与画布偏好，并绑定到当前工作区。`));
    const list=el('div','template-list');content.appendChild(list);
    const d1=el('div','template-card');append(d1,el('strong','','任务树拓扑变更'),el('span','',diff.tree_leaves_change||'无'));list.appendChild(d1);
    const d2=el('div','template-card');append(d2,el('strong','','协调者变更'),el('span','',diff.director_change||'无'));list.appendChild(d2);
    const d3=el('div','template-card');append(d3,el('strong','','画布便签变更'),el('span','',diff.sticky_notes_change||'无'));list.appendChild(d3);
    const actions=el('div','dialog-action-row'),cancel=el('button','button','取消'),confirm=el('button','button primary','确认还原并绑定当前路径');
    cancel.type=confirm.type='button';
    cancel.addEventListener('click',closeDialog);
    confirm.addEventListener('click',async()=>{
      confirm.disabled=true;
      try{
        const r=await call('restore_backup',{confirm:true,path:res.path,preview_id:preview.preview_id});
        if(r?.forest)setSnapshot(r);else await refresh();
        closeDialog();
        toast('备份已成功还原并绑定到当前工作区！');
      }catch(e){toast(e.message,true);confirm.disabled=!online;}
    });
    append(actions,cancel,confirm);content.appendChild(actions);
  }
  function showScannedTrees(data) {
    const content=openDialog('当前工程中的任务树','文件 · 扫描结果');
    append(content,el('p','panel-intro','已读取当前工程目录、templates 与成果目录。先预览，再明确确认导入；当前正在使用的任务树不会被覆盖。'));
    const candidates=Array.isArray(data?.candidates)?data.candidates:[],list=el('div','template-list');content.appendChild(list);
    candidates.forEach(candidate=>{
      const card=el('article','tree-scan-card');
      append(card,el('strong','',candidate.title||candidate.path||'任务树文件'),el('span','',candidate.active?'当前工程正在使用':`${candidate.branch_count??0} 个分支 · ${candidate.leaf_count??0} 项任务`),el('small','',candidate.path||''));
      (candidate.warnings||[]).forEach(warning=>card.appendChild(el('small','import-warning',warning)));
      if(candidate.error){card.appendChild(el('p','error-note',candidate.error));}
      else if(candidate.active){card.appendChild(el('p','panel-intro','这棵树已经显示在画布上。'));
      }else{
        const open=el('button','button small','预览并导入');open.type='button';open.addEventListener('click',()=>previewTreeImport({path:candidate.path}));card.appendChild(open);
      }
      list.appendChild(card);
    });
    if(!candidates.length)list.appendChild(el('p','panel-empty','当前工程没有找到任务树文件。可以选择其他位置的任务树。'));
    if(data?.truncated)list.appendChild(el('p','import-warning','目录内容较多，已达到扫描上限。可将任务树放在工程根目录或 templates / 成果 子目录后重新扫描。'));
    const actions=el('div','dialog-action-row'),choose=el('button','button primary','从其他位置选择文件');choose.type='button';choose.disabled=!online;choose.addEventListener('click',()=>previewTreeImport({}));actions.appendChild(choose);content.appendChild(actions);
  }
  async function scanProjectTrees({startup=false}={}) {
    try{
      const data=await call('scan_project_trees',{}),candidates=Array.isArray(data?.candidates)?data.candidates:[],active=candidates.find(item=>item.active),reusable=candidates.filter(item=>!item.active&&!item.error);
      if(startup){
        if(reusable.length)toast(active?`已自动打开当前任务树；另发现 ${reusable.length} 个可复用任务树。`:`发现 ${reusable.length} 个可复用任务树。`,false,()=>showScannedTrees(data));
      }else showScannedTrees(data);
      return data;
    }catch(error){if(startup)toast('当前工程任务树自动扫描失败：'+error.message,true);else toast(error.message,true);throw error;}
  }
  async function workspaceDropped() {
    lastScannedWorkspace='';
    try{await refresh();closeDialog();requestAnimationFrame(()=>{fit();});toast(`🎉 项目目录已切换至：${projectName()}，专属任务树与工位已就绪！开始新任务。`);}
    catch(error){toast('文件夹已拖入，但项目状态读取失败：'+error.message,true);}
  }
  function normalize(data) {
    data=data&&typeof data==='object'?data:{};
    const forest=data.forest||data.tree||data.snapshot||data;
    const config=data.config&&typeof data.config==='object'?data.config:{};
    let members=data.members||[];
    if(!Array.isArray(members))members=Object.entries(members).map(([id,value])=>({...value,id}));
    if(!members.length && config.workers)members=Object.entries(config.workers).map(([id,value])=>({...value,id}));
    members=members.map(m=>typeof m==='string'?{id:m,name:m,enabled:true}:({...m,id:string(m.id||m.agent||m.agent_id||m.key||m.name),name:string(m.display_name||m.name||m.id||m.agent),enabled:m.enabled!==false})).filter(m=>m.id);
    const roots=Array.isArray(forest.roots)?forest.roots:[],branches=Array.isArray(forest.branches)?forest.branches:[],leaves=Array.isArray(forest.leaves)?forest.leaves:[];
    for(const leaf of leaves)if(leaf.owner && !members.some(m=>m.id===leaf.owner))members.push({id:string(leaf.owner),name:string(leaf.owner),enabled:true,observed:true});
    return {forest:{...forest,roots,branches,leaves},config:{...config},members,project:{...(data.project||{})},ui_state:data.ui_state&&typeof data.ui_state==='object'?{...data.ui_state}:null,pending_goals:Array.isArray(data.pending_goals)?data.pending_goals:[],skills:data.skills,recovery:data.recovery,activity:data.activity};
  }
  function projectName() {const p=snapshot.project,c=snapshot.config;return string(p.name||p.title||c.project_name||c.name||string(c.workspace||p.workspace).replace(/[\\/]+$/,'').split(/[\\/]/).pop()||'当前项目');}
  const memberName = id => {const member=snapshot.members.find(m=>m.id===id);return member?.name||member?.display_name||member?.label||string(id)||'尚未分配';};
  const working = leaf => leaf.state==='running';
  function shortTitle(value,max=24) {let text=string(value).replace(/\b(?:m|j|r)-[a-f\d-]{14,}\b/gi,'').trim();if(!text)text='未命名任务';return text.length>max?text.slice(0,max)+'…':text;}
  function setSnapshot(data, requestGen = null) {
    if (!data || typeof data !== 'object') return false;
    if (requestGen !== null && requestGen < mutationGeneration) {
      lastRejectedSnapshot = {
        reason: 'stale_generation',
        requestGen,
        currentGen: mutationGeneration,
        at: new Date().toISOString()
      };
      console.warn(`[SnapshotGuard] Stale snapshot rejected: requestGen ${requestGen} < mutationGeneration ${mutationGeneration}`);
      return false;
    }
    const incomingTime = data?.forest?.generated_at || data?.project?.generated_at;
    const currentTime = snapshot?.forest?.generated_at || snapshot?.project?.generated_at;
    const previousWorkspace = snapshot?.project?.workspace || snapshot?.config?.workspace;
    const incomingWorkspace = data?.project?.workspace || data?.config?.workspace;
    const workspaceChanged = Boolean(previousWorkspace && incomingWorkspace && incomingWorkspace !== previousWorkspace);

    if (!workspaceChanged && incomingTime && currentTime && incomingTime < currentTime) {
      lastRejectedSnapshot = {
        reason: 'stale_timestamp',
        incomingTime,
        currentTime,
        at: new Date().toISOString()
      };
      console.warn(`[SnapshotGuard] Stale snapshot rejected: incomingTime ${incomingTime} < currentTime ${currentTime}`);
      return false;
    }

    const activeElement=document.activeElement,focusMember=activeElement?.dataset?.member||'',focusLeaf=activeElement?.dataset?.leafId||'';
    const previousLeaves=new Map(snapshot.forest.leaves.map(leaf=>[leaf.id,leaf])),hadSnapshot=ready;snapshot=normalize(data);ready=true;const f=snapshot.forest,l=f.leaves;
    const nextCollapseScope=string(snapshot.project.workspace||snapshot.config.workspace||'default');
    if(nextCollapseScope!==collapseScope){collapseScope=nextCollapseScope;collapsedBranches.clear();const hostSaved=snapshot.ui_state?.collapsed_branches;if(Array.isArray(hostSaved)){hostSaved.filter(value=>typeof value==='string').forEach(value=>collapsedBranches.add(value));}else{try{const saved=JSON.parse(localStorage.getItem('agentbus.collapsedBranches:'+collapseScope)||'[]');if(Array.isArray(saved))saved.filter(value=>typeof value==='string').forEach(value=>collapsedBranches.add(value));}catch{}}}
    if(workspaceChanged){clearHistory();detailDrafts.clear();selectedLeaf='';$('detailContent').dataset.leafId='';$('detailDrawer').hidden=true;initializedView=false;scale=1;panX=30;panY=30;viewportSize={width:0,height:0};stopAutomaticDispatch();focusedMember='';filter='all';searchQuery='';if($('leafSearch'))$('leafSearch').value='';}
    if(autoDispatchActive&&(f.stopped||assignmentMode()!=='automatic'))stopAutomaticDispatch();
    if(!hadSnapshot)$('projectFlow').open=snapshot.project.configured!==true||!l.length;
    const initialSnapshot=!hadSnapshot||workspaceChanged;
    pendingTransitions=new Map(l.map(leaf=>{const previous=previousLeaves.get(leaf.id);return[leaf.id,isCommittedDelivery(previous,leaf,initialSnapshot)?'result-arrived':transitionMotion(previous?.state,leaf.state,initialSnapshot)];}).filter(([,motion])=>motion));
    // §6.6 交付完成触发轻脆提示音与原生 Toast 通知
    if (!initialSnapshot) {
      for (const leaf of l) {
        if (pendingTransitions.get(leaf.id) === 'result-arrived') {
          triggerDeliveryNotice(leaf);
        }
      }
    }
    $('headerProjectNameVisible').textContent=projectName();const spName=$('sidebarProjectName');if(spName){spName.textContent=projectName();spName.style.cursor='pointer';spName.title='点击切换项目工作空间';if(!spName.dataset.boundSwitch){spName.dataset.boundSwitch='1';spName.addEventListener('click',()=>{$('menuWorkspaceFolder')?.click();});}}
    const done=l.filter(n=>n.state==='delivered').length;
    const working=l.filter(n=>n.state==='running').length;
    const claimable=l.filter(n=>n.claimable).length;
    // 状态镜像：可见画布承载真源，测试与可访问性都读这里（不再依赖隐藏兼容节点）。
    const mirror=$('forestViewport');
    if(mirror){
      mirror.dataset.deliveredCount=String(done);
      mirror.dataset.totalCount=String(l.length);
      mirror.dataset.workingCount=String(working);
      mirror.dataset.claimableCount=String(claimable);
      mirror.dataset.assignmentMode=assignmentMode();
      mirror.dataset.projectName=projectName();
      mirror.dataset.forestTitle=f.roots.length>1?projectName()+' · '+f.roots.length+' 棵任务树':shortTitle(f.roots[0]?.title||projectName(),32);
      mirror.dataset.source=f.observation_errors?.length?'记录存在读取问题':'状态来自正式总线';
    }
    if ($('navLeafCount')) $('navLeafCount').textContent=l.length;
    const pct = l.length ? Math.round((done / l.length) * 100) : 0;
    if ($('capsuleFill')) $('capsuleFill').style.width = pct + '%';
    if ($('capsuleText')) $('capsuleText').textContent = `${done}/${l.length} 已交付 · ${pct}%`;
    const capsule=$('progressCapsule');
    if(capsule){
      capsule.dataset.deliveredCount=String(done);
      capsule.dataset.totalCount=String(l.length);
      capsule.dataset.workingCount=String(working);
      capsule.dataset.claimableCount=String(claimable);
      capsule.dataset.percent=String(pct);
    }
    $('forestSubtitle').textContent=l.length?`${l.length} 项任务 · ${done} 份正式交付${snapshot.pending_goals.length?' · '+snapshot.pending_goals.length+' 个新目标待分解':''}。按序号找到任务，沿成果和接手说明继续。`:'目标、成果与接手说明来自正式记录。';
    const supervisorStatus=$('menuSupervisorStatus');
    if(supervisorStatus){const enabled=Boolean(snapshot.config.supervision?.strict_supervision);supervisorStatus.textContent=enabled?'已开启':'未开启';supervisorStatus.classList.toggle('enabled',enabled);supervisorStatus.setAttribute('aria-label',enabled?'独立成果复核已开启':'独立成果复核未开启');}
    const when=f.generated_at||snapshot.project.generated_at;
    $('snapshotTime').textContent=when?'快照 '+formatDate(when):'快照未提供生成时间';
    $('snapshotTime').title=string(when);$('sourceLabel').textContent=f.observation_errors?.length?'记录存在读取问题':'状态来自正式总线';
    if ($('stopButton')) {
      const stopSpan = $('stopButton').querySelector('span');
      if (stopSpan) stopSpan.textContent = f.stopped ? '恢复派工' : '暂停新派发';
      const stopIcon = $('stopButton').querySelector('use');
      if (stopIcon) stopIcon.setAttribute('href', f.stopped ? '#i-play' : '#i-pause');
      $('stopButton').title = f.stopped ? '恢复就绪任务派发与自动流转' : '暂停新任务派发；保留在途任务继续执行，不终止外部正在运行的会话';
    }
    if(focusedMember&&!snapshot.members.some(m=>m.id===focusedMember))focusedMember='';
    renderMembers();renderForest();renderDeadlockBanner();pendingTransitions.clear();
    if(focusMember)[...$('memberList').querySelectorAll('[data-member]')].find(node=>node.dataset.member===focusMember)?.focus({preventScroll:true});
    else if(focusLeaf)[...$('forestNodes').querySelectorAll('[data-leaf-id]')].find(node=>node.dataset.leafId===focusLeaf)?.focus({preventScroll:true});
    if(selectedLeaf){const leaf=l.find(n=>n.id===selectedLeaf);if(leaf)renderDetail(leaf);else closeDetail();}
    if(!initializedView&&(l.length||snapshot.pending_goals.length)){initializedView=true;const scope=collapseScope;requestAnimationFrame(async()=>{if(!await restoreWorkspaceViewport())requestAnimationFrame(()=>{if(scope===collapseScope)fit();});});}
    if(hadSnapshot&&!workspaceChanged)notifyDependencyReady(previousLeaves,l);
    if(snapshot.project.configured===true&&(!lastScannedWorkspace||workspaceChanged)){lastScannedWorkspace=nextCollapseScope;scanProjectTrees({startup:true}).catch(()=>{});}
    setConnection(online);
    return inspect();
  }
  function renderDeadlockBanner() {
    const banner = $('deadlockBanner');
    if (!banner) return;
    const diag = snapshot.forest?.deadlock_diagnostics;
    const blockers = Array.isArray(diag?.root_blockers) ? diag.root_blockers : [];
    if (!diag?.is_deadlocked || !blockers.length) {
      banner.hidden = true;
      banner.replaceChildren();
      return;
    }
    banner.hidden = false;
    banner.replaceChildren();
    banner.style.cssText = 'margin: 0 16px 8px; padding: 10px 14px; border-radius: 8px; background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.45); display: flex; flex-direction: column; gap: 6px; font-size: 13px;';
    const titleRow = el('div', 'deadlock-banner-title');
    titleRow.style.cssText = 'display: flex; align-items: center; justify-content: space-between; font-weight: 600; color: #dc2626;';
    titleRow.appendChild(el('span', '', `🚨 流水线死锁告警：无在途任务，${blockers.length} 个根阻断节点卡住了后续任务推进！`));
    banner.appendChild(titleRow);
    blockers.slice(0, 3).forEach(b => {
      const item = el('div', 'deadlock-blocker-item');
      item.style.cssText = 'display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; padding: 4px 8px; border-radius: 6px; background: rgba(255,255,255,0.5);';
      const sec = b.section_no ? `§${b.section_no} ` : '';
      const errText = b.evidence_error || b.blocked_reason || b.state || '受阻';
      const info = el('span', '', `${sec}${b.title || b.id} — 原因：${errText}（阻断下游 ${b.blocked_downstream_count || 0} 项）`);
      const btnGroup = el('div', 'deadlock-blocker-actions');
      btnGroup.style.cssText = 'display: flex; gap: 6px; align-items: center;';
      const locateBtn = el('button', 'button small', '定位节点');
      locateBtn.type = 'button';
      locateBtn.addEventListener('click', () => locateCard(b.id));
      const resetBtn = el('button', 'button small primary', '🔄 一键重置重排');
      resetBtn.type = 'button';
      resetBtn.disabled = !online;
      resetBtn.title = '重排受阻任务；仍受原期限、预算和重试限制';
      resetBtn.addEventListener('click', () => {const current=snapshot.forest.leaves.find(leaf=>leaf.id===b.id);if(!current)return toast('任务状态已变化，请刷新后重试。',true);confirmStoppedMember(current, binding => mutate(resetBtn, 'reset_leaf', { leaf_id: binding.leaf_id, reason: '工作台死锁横幅重置受阻节点', owner_confirmed_inactive: true, expected_task_id: binding.task_id, expected_owner: binding.owner, expected_claim_epoch: binding.claim_epoch }));});
      const passBtn = el('button', 'button small', '✅ 仲裁放行');
      passBtn.type = 'button';
      passBtn.disabled = !online;
      passBtn.title = '允许下游继续检查其余前序条件，保留本项的成果核验状态';
      if (['awaiting_audit', 'rejected'].includes(b.state)) passBtn.disabled = true;
      passBtn.addEventListener('click', () => mutate(passBtn, 'pass_leaf', { leaf_id: b.id, reason: '工作台死锁横幅管理员确认放行' }));
      append(btnGroup, locateBtn, resetBtn, passBtn);
      append(item, info, btnGroup);
      banner.appendChild(item);
    });
  }
  function formatDate(raw) {const date=new Date(raw);return Number.isNaN(date.getTime())?string(raw):date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
  let memberRenderKey='';
  function renderMembers() {
    const key=JSON.stringify([focusedMember,snapshot.project.workspace,snapshot.config.director,snapshot.members,snapshot.forest.leaves.map(leaf=>[leaf.id,leaf.owner,leaf.state,leaf.section_no,leaf.title,leaf.conversation_id])]);
    if(key===memberRenderKey)return;memberRenderKey=key;
    const list=$('memberList');list.replaceChildren();const liveLabels=[];
    const modeLabels={file:'文件总线',cli:'独立 CLI',acp:'ACP 协议',sdk:'SDK 宿主'};
    snapshot.members.filter(member=>member.enabled).forEach(member=>{
      const leaves=snapshot.forest.leaves.filter(leaf=>leaf.owner===member.id),presence=presenceFor(member,leaves),done=leaves.filter(leaf=>leaf.state==='delivered').length,activeConversationIds=[...new Set(leaves.filter(leaf=>['claimed','running'].includes(leaf.state)&&leaf.conversation_id).map(leaf=>leaf.conversation_id))];
      const button=el('button','member-item agent-role-'+member.id.toLowerCase()+(focusedMember===member.id?' selected':''));button.type='button';button.dataset.member=member.id;button.dataset.presence=presence.mode;button.setAttribute('aria-pressed',String(focusedMember===member.id));
      const current=leaves.find(leaf=>leaf.state==='running');
      const status=presence.mode==='working'?'执行心跳已确认':presence.mode==='blocked'?'存在受阻任务':presence.mode==='claimed'?'任务已认领，等待心跳':presence.mode==='completed'?'分配任务已交付':presence.mode==='disabled'?'成员已停用':done?done+' 项正式交付':'待命 · 暂无执行心跳';
      button.title=memberName(member.id)+' · '+status;
      const mark=el('span','member-logo agent-brand-'+member.id.toLowerCase()+' '+presence.mode);append(mark,memberLogo(member.id,member),el('span','member-presence-light'));
      const info=el('span','member-info'),headerRow=el('span','member-header-row'),name=el('span','member-name',memberName(member.id));
      if(member.id===snapshot.config.director)name.appendChild(el('span','director-label','协调者'));
      if(presence.mode==='working'){const typing=el('span','typing-indicator');typing.title='此成员正在执行任务（心跳活跃）';typing.setAttribute('aria-label','正在工作');append(typing,el('span','dot'),el('span','dot'),el('span','dot'));name.appendChild(typing);}
      const modeKey=string(member.communication_mode||member.transport||'file').toLowerCase();
      const modePill=el('span','member-mode-pill mode-'+modeKey,modeLabels[modeKey]||modeLabels.file);
      append(headerRow,name,modePill);
      let note=presenceNote(presence);
      if(current)note='正在处理 '+(taskNumber(current)?taskNumber(current)+' · ':'')+shortTitle(current.title,24);
      const metaRow=el('span','member-meta-row');append(metaRow,el('span','member-role-tag',member.id===snapshot.config.director?'项目架构协调者':'协同执行成员'),el('span','member-count',leaves.length+' 项 · '+done+' 已交付'));if(activeConversationIds.length)metaRow.appendChild(el('span','member-count',activeConversationIds.length+' 个独立对话'));
      append(info,headerRow,el('span','member-note',note),metaRow);append(button,mark,info,memberActivity(member,presence));
      button.addEventListener('click',()=>{focusedMember=focusedMember===member.id?'':member.id;renderMembers();applyFocus();[...list.querySelectorAll('.member-item')].find(item=>item.dataset.member===member.id)?.focus();});
      const row=el('div','member-card-row');row.appendChild(button);const onboarding=el('button','member-onboard-button','接入指令');onboarding.type='button';onboarding.title='生成 '+memberName(member.id)+' 的初始接入指令';onboarding.setAttribute('aria-label',memberName(member.id)+'：生成初始接入指令');onboarding.disabled=!online;onboarding.addEventListener('click',()=>showMemberPrompt(member.id));row.appendChild(onboarding);
      const triggerBtn=el('button','member-onboard-button trigger-wizard-btn','连接');
      triggerBtn.type='button';
      triggerBtn.title='配置 '+memberName(member.id)+' 的桌面连接与自动唤醒向导';
      triggerBtn.setAttribute('aria-label',memberName(member.id)+'：桌面连接向导');
      triggerBtn.style.marginLeft='3px';
      triggerBtn.disabled=!online;
      triggerBtn.addEventListener('click',()=>showTriggerOnboardingDialog(member.id));
      row.appendChild(triggerBtn);
      list.appendChild(row);
      liveLabels.push(memberName(member.id)+'：'+status);
    });
    const addOnboardBtn=el('button','button small add-member-quick-btn','+ 接入新智能体');addOnboardBtn.type='button';addOnboardBtn.style.margin='8px 0 4px 0';addOnboardBtn.style.width='100%';addOnboardBtn.title='一键接入新智能体并生成开箱即用接入指令';addOnboardBtn.addEventListener('click',()=>showOneClickOnboardDialog());list.appendChild(addOnboardBtn);
    if(!snapshot.members.length)list.appendChild(el('p','member-hint','快照尚未提供成员记录。'));
    $('clearFocus').hidden=!focusedMember;$('focusLabel').hidden=!focusedMember;$('focusLabel').textContent=focusedMember?'聚焦 '+memberName(focusedMember):'';
    const announcement='成员状态：'+(liveLabels.join('；')||'暂无已启用成员');
    if(announcement!==lastPresenceAnnouncement){$('presenceAnnouncement').textContent=announcement;lastPresenceAnnouncement=announcement;}
  }
  function groupFor(leaf) {
    const nodes=new Map(snapshot.forest.branches.map(n=>[n.id,n]));let parent=nodes.get(leaf.parent_id),seen=new Set();
    while(parent&&!seen.has(parent.id)){seen.add(parent.id);const hint=string(parent.id+' '+parent.title).toLowerCase();
      if(/desktop|interface|\bui\b|桌面|交互|看板/.test(hint))return'desktop';
      if(/skills?|members?|memory|extension|技能|成员|记忆|扩展|计划/.test(hint))return'skills';
      if(/reliab|safety|evidence|防撞|可靠|证据/.test(hint))return'reliability';
      if(/release|build|integrat|发布|打包|集成/.test(hint))return'release';
      parent=nodes.get(parent.parent_id);
    }
    return'skills';
  }
  function rootForLeaf(leaf) {
    const roots=snapshot.forest.roots,branches=new Map(snapshot.forest.branches.map(branch=>[branch.id,branch]));let parent=leaf.parent_id,seen=new Set();
    while(parent&&!seen.has(parent)){seen.add(parent);if(roots.some(root=>root.id===parent))return parent;parent=branches.get(parent)?.parent_id;}
    return roots.length===1?roots[0].id:'';
  }
  function forestLayout() {
    const bands=[],rootLayouts=[];const branches=snapshot.forest.branches,byId=new Map(branches.map(branch=>[branch.id,branch]));
    const roots=snapshot.forest.roots.length?snapshot.forest.roots:[{id:'unscoped',title:projectName(),goal:snapshot.project.goal||'项目工作'}];
    roots.forEach(root=>{
      const children=snapshot.forest.leaves.filter(leaf=>rootForLeaf(leaf)===root.id||roots.length===1);
      const hasExplicitBranches=branches.some(branch=>branch.parent_id===root.id);
      let rootBands;
      if(!hasExplicitBranches&&groups.length){
        rootBands=groups.map(group=>({...group,root,children:children.filter(leaf=>groupFor(leaf)===group.id)}));
      }else{
        const direct=branches.filter(branch=>branch.parent_id===root.id);
        const nearest=leaf=>{
          let branch=byId.get(leaf.parent_id||leaf.branch),last=branch,seen=new Set();
          while(branch&&!seen.has(branch.id)){
            seen.add(branch.id);
            last=branch;
            if(branch.parent_id===root.id)break;
            branch=byId.get(branch.parent_id);
          }
          return last?.id;
        };
        rootBands=direct.map(branch=>({id:branch.id,title:branch.title,section_no:branch.section_no,root,children:children.filter(leaf=>nearest(leaf)===branch.id)}));
        const assigned=new Set(rootBands.flatMap(band=>band.children.map(leaf=>leaf.id))),unplaced=children.filter(leaf=>!assigned.has(leaf.id));
        if(unplaced.length||!rootBands.length)rootBands.push({id:root.id+'-tasks',title:'项目工作',root,children:unplaced});
      }
      rootLayouts.push({root,children});
      rootBands.forEach(band=>bands.push({...band}));
    });
    return{bands,rootLayouts};
  }
  function positioned(node,x,y,w,h) {Object.assign(node.style,{left:x+'px',top:y+'px'});node.dataset.x=x;node.dataset.y=y;node.dataset.width=w;node.dataset.height=h;return node;}
  function leafCardHeight(leaf) {const name=memberName(leaf.owner);const estimatedWidth=Array.from(name).reduce((sum,char)=>sum+(char.codePointAt(0)>255?12:7),0),hasProgress=leaf.progress_percent!=null||typeof leaf.progress==='number'||leaf.progress?.percent!=null;return 90+Math.max(0,Math.ceil(estimatedWidth/165)-1)*16+(hasProgress?9:0);}
  const branchCollapseTimers=new Map(),BRANCH_FOLD_MS=260;
  function reducedMotion() {try{return Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);}catch{return false;}}
  function persistCollapsedBranches() {const branchIds=[...collapsedBranches];try{localStorage.setItem('agentbus.collapsedBranches:'+collapseScope,JSON.stringify(branchIds));}catch{}if(snapshot.ui_state)snapshot.ui_state.collapsed_branches=branchIds;if(bridge()&&online&&collapseScope){void call('save_branch_folds',{workspace:collapseScope,branch_ids:branchIds}).catch(error=>toast('分支折叠状态未能保存：'+error.message,true));}}
  function branchElements(id) {return [...document.querySelectorAll('.branch-node')].find(node=>node.dataset.group===id)||null;}
  function hasBranchAncestor(node,id) {try{return JSON.parse(node.dataset.foldAncestors||'[]').includes(id);}catch{return false;}}
  function branchCards(id) {return [...document.querySelectorAll('.leaf-card')].filter(node=>hasBranchAncestor(node,id));}
  function branchSubnodes(id) {return [...document.querySelectorAll('.branch-node')].filter(node=>node.dataset.group!==id&&hasBranchAncestor(node,id));}
  function branchFoldEdges(id) {return [...document.querySelectorAll('.forest-lines path')].filter(node=>node.dataset.groupId&&hasBranchAncestor(node,id)&&node.getAttribute('data-collapsible-edge')==='true');}
  // §8.11 抽取：嵌套分支节点与其可折叠连线的公共数据集标记（两处渲染共用）。
  // 与抽取前逐字等价——每个调用点传入自己的 foldAncestors，不改变既有语义。
  function tagBranchNode(node, scopeGroupId, branchId, foldAncestors, rootId, leafIds) {
    node.dataset.group = branchId;
    node.dataset.groupId = scopeGroupId;
    node.dataset.foldAncestors = JSON.stringify(foldAncestors);
    node.dataset.rootId = rootId;
    node.dataset.leafIds = JSON.stringify(leafIds);
    return node;
  }
  function tagCollapsibleEdge(path, scopeGroupId, foldAncestors) {
    path.dataset.groupId = scopeGroupId;
    path.dataset.foldAncestors = JSON.stringify(foldAncestors);
    path.setAttribute('data-collapsible-edge', 'true');
    return path;
  }
  function foldDelta(node,branch) {
    const bx=Number(branch.dataset.x||0),by=Number(branch.dataset.y||0),bw=Number(branch.dataset.width||300),bh=Number(branch.dataset.height||60);
    const x=Number(node.dataset.x||0),y=Number(node.dataset.y||0),w=Number(node.dataset.width||0),h=Number(node.dataset.height||0);
    node.style.setProperty('--fold-x',(bx+bw/2-x-w/2)+'px');node.style.setProperty('--fold-y',(by+bh/2-y-h/2)+'px');
  }
  function edgeLength(path) {try{const length=Number(path.getTotalLength?.());if(Number.isFinite(length)&&length>0)return length;}catch{}return 120;}
  function animateBranchExpansion(id) {
    if(reducedMotion())return;const branch=branchElements(id);if(!branch)return;
    const cards=branchCards(id),edges=branchFoldEdges(id);
    cards.forEach(card=>{foldDelta(card,branch);card.classList.add('unfolding-from-branch');void card.getBoundingClientRect();requestAnimationFrame(()=>{card.classList.add('unfolding-active');requestAnimationFrame(()=>card.classList.remove('unfolding-from-branch'));setTimeout(()=>card.classList.remove('unfolding-active'),BRANCH_FOLD_MS+20);});});
    branchSubnodes(id).forEach(node=>{foldDelta(node,branch);node.classList.add('unfolding-from-branch');void node.getBoundingClientRect();requestAnimationFrame(()=>{node.classList.add('unfolding-active');requestAnimationFrame(()=>node.classList.remove('unfolding-from-branch'));setTimeout(()=>node.classList.remove('unfolding-active'),BRANCH_FOLD_MS+20);});});
    edges.forEach(path=>{const length=edgeLength(path);path.style.strokeDasharray=String(length);path.style.strokeDashoffset=String(length);path.classList.add('unfolding-from-branch');void path.getBoundingClientRect();requestAnimationFrame(()=>{path.classList.add('unfolding-active');requestAnimationFrame(()=>{path.style.strokeDashoffset='0';path.classList.remove('unfolding-from-branch');});setTimeout(()=>path.classList.remove('unfolding-active'),BRANCH_FOLD_MS+20);});});
  }
  function setBranchCollapsed(id,collapsed) {
    const prior=branchCollapseTimers.get(id);if(prior){clearTimeout(prior);branchCollapseTimers.delete(id);}
    if(!collapsed){collapsedBranches.delete(id);persistCollapsedBranches();renderForest();animateBranchExpansion(id);return;}
    collapsedBranches.add(id);persistCollapsedBranches();const branch=branchElements(id);
    if(!branch||reducedMotion()){renderForest();return;}
    const toggle=branch.querySelector('.branch-collapse-button'),meta=branch.querySelector('.branch-meta'),summary=branch.querySelector('.branch-collapse-summary');
    toggle?.setAttribute('aria-expanded','false');toggle?.setAttribute('aria-label','展开分支 '+(branch.title||''));if(toggle){toggle.title='展开此分支中的任务';toggle.classList.add('collapsed');toggle.innerHTML='<svg viewBox="0 0 16 16" class="fold-icon"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';toggle.disabled=true;}if(meta)meta.hidden=true;if(summary)summary.hidden=false;
    const bx=Number(branch.dataset.x||0),by=Number(branch.dataset.y||0),bw=Number(branch.dataset.width||300),bh=Number(branch.dataset.height||60);
    branchCards(id).forEach(card=>{foldDelta(card,branch);card.classList.add('folding-into-branch');});
    branchSubnodes(id).forEach(node=>{foldDelta(node,branch);node.classList.add('folding-into-branch');});
    branchFoldEdges(id).forEach(path=>{const length=edgeLength(path);path.style.strokeDasharray=String(length);path.style.strokeDashoffset='0';void path.getBoundingClientRect();path.classList.add('folding-into-branch');requestAnimationFrame(()=>{path.style.strokeDashoffset=String(length);});});
    const timer=setTimeout(()=>{branchCollapseTimers.delete(id);renderForest();},BRANCH_FOLD_MS+40);branchCollapseTimers.set(id,timer);
  }
  function line(x1,y1,x2,y2,mode='',leafIds=[],edge='pending') {
    const path=document.createElementNS(NS,'path'),span=Math.max(55,(x2-x1)*.48);
    path.setAttribute('d',`M ${x1} ${y1} C ${x1+span} ${y1}, ${x2-span} ${y2}, ${x2} ${y2}`);
    path.setAttribute('vector-effect','non-scaling-stroke');
    path.setAttribute('class',mode);path.setAttribute('data-edge',edge);path.dataset.leafIds=JSON.stringify(leafIds);$('forestLines').appendChild(path);
  }
  function treeEdge(d,edge,leaves,state='') {
    const path=document.createElementNS(NS,'path');path.setAttribute('d',d);path.setAttribute('vector-effect','non-scaling-stroke');
    path.setAttribute('class',state);path.setAttribute('data-edge',edge);path.dataset.leafIds=JSON.stringify(leaves.map(leaf=>leaf.id));$('forestLines').appendChild(path);
    // A separate decorative layer preserves the solid spline and never changes bus state.
    if(edge!=='dep-wire'&&!reducedMotion()){
      const receipt=leaves.some(leaf=>pendingTransitions.get(leaf.id)==='result-arrived');
      const flow=document.createElementNS(NS,'path');
      const mode=receipt?'delivery-current ltr-reverse-pulse':state.split(' ').includes('running')?'running-current ltr-flow-ants':'ambient-current';
      flow.setAttribute('class','edge-flow '+mode);flow.setAttribute('aria-hidden','true');flow.setAttribute('vector-effect','non-scaling-stroke');
      if(receipt)flow.addEventListener('animationend',event=>{
        if(event.target===flow&&event.animationName==='ltr-reverse-pulse')flow.classList.remove('delivery-current','ltr-reverse-pulse');
      });
      flow.style.setProperty('--pulse-delay',edge==='leaf'?'0s':edge==='branch'?'.7s':'1.35s');
      if(!receipt){const duration=mode.includes('running-current')?1.6:18;flow.style.setProperty('--flow-phase',(-(Date.now()/1000%duration))+'s');}
      path._flow=flow;$('forestLines').appendChild(flow);syncEdgeFlow(path);
    }
    return path;
  }
  function syncEdgeFlow(path) {
    const flow=path?._flow;if(!flow)return;
    flow.setAttribute('d',path.getAttribute('d'));
    for(const [key,value] of Object.entries(path.dataset))if(key!=='edge')flow.dataset[key]=value;
    // Motion paths have no data-edge: geometry queries must select the physical edge once.
    flow.style.setProperty('--path-length',edgeLength(path)+'px');
  }
  function syncAllEdgeFlows() {document.querySelectorAll('.forest-lines path').forEach(syncEdgeFlow);}
  const edgeState=leaves=>leaves.some(leaf=>leaf.state==='running')?'running':leaves.some(leaf=>leaf.state==='blocked'&&!leaf.unmet_dependencies?.length)?'blocked':leaves.some(leaf=>leaf.state==='claimed')?'claimed':leaves.length&&leaves.every(leaf=>leaf.state==='delivered')?'delivered':'';

  // =========================================================================
  // 空间化无限笔记式任务白板：持久化坐标、自由拖拽、交互连线与 DIY 分组工具函数
  // =========================================================================
  const SPATIAL_KEY_PREFIX = 'agentbus.spatialLayout:';
  let suppressCardClick = false;

  function spatialScope() {
    return string(snapshot.project.workspace||snapshot.config.workspace||'default');
  }
  function loadSpatialLayout() {
    try {
      const raw = localStorage.getItem(SPATIAL_KEY_PREFIX + spatialScope());
      return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
  }
  function saveSpatialNode(id, pos) {
    try {
      const layout = loadSpatialLayout();
      if (pos) layout[id] = pos;
      else delete layout[id];
      localStorage.setItem(SPATIAL_KEY_PREFIX + spatialScope(), JSON.stringify(layout));
    } catch {}
  }
  function clearAllSpatialLayout() {
    try {
      localStorage.removeItem(SPATIAL_KEY_PREFIX + spatialScope());
    } catch {}
  }
  function calcHorizontalBezier(x1, y1, x2, y2) {
    const span = Math.max(30, Math.abs(x2 - x1) * 0.5);
    return `M ${x1} ${y1} C ${x1 + span} ${y1}, ${x2 - span} ${y2}, ${x2} ${y2}`;
  }

  // =========================================================================
  // §8.6 撤销/重做系统（Undo / Redo Stack with Ctrl+Z & Ctrl+Y）
  // 记录最近 N 步空间布局拖拽、便签创建/删除/移动、智能布局对齐等操作，支持一键 Ctrl+Z 还原
  // =========================================================================
  const undoStack = [];
  const redoStack = [];
  const MAX_UNDO_STACK = 50;
  let historyBusy = false, historyVersion = 0;

  function clearHistory() {
    undoStack.length = 0;
    redoStack.length = 0;
    historyVersion++;
  }

  function pushUndoAction(action) {
    if (!action || !action.type) return;
    undoStack.push(action);
    if (undoStack.length > MAX_UNDO_STACK) undoStack.shift();
    redoStack.length = 0;
    historyVersion++;
  }

  async function applyAction(action, isUndo) {
    if (!action) throw new Error('操作记录缺失');
    switch (action.type) {
      case 'SPATIAL_MOVE': {
        const targetPos = isUndo ? action.oldPos : action.newPos;
        saveSpatialNode(action.id, targetPos);
        renderForest();
        toast(isUndo ? '已撤销移动 (Ctrl+Z)' : '已重做移动 (Ctrl+Y)');
        break;
      }
      case 'STICKY_MOVE': {
        const targetPos = isUndo ? action.oldPos : action.newPos;
        updateStickyNote(action.id, targetPos);
        renderStickyNotes();
        toast(isUndo ? '已撤销便签移动 (Ctrl+Z)' : '已重做便签移动 (Ctrl+Y)');
        break;
      }
      case 'STICKY_DELETE': {
        if (isUndo) {
          const notes = loadStickyNotes();
          if (!notes.some(n => n.id === action.note.id)) {
            notes.push(action.note);
            saveStickyNotes(notes);
            renderStickyNotes();
          }
          toast('已撤销删除：便签已恢复 (Ctrl+Z)');
        } else {
          removeStickyNote(action.note.id);
          renderStickyNotes();
          toast('已重做删除：便签已移除 (Ctrl+Y)');
        }
        break;
      }
      case 'STICKY_CREATE': {
        if (isUndo) {
          removeStickyNote(action.id);
          renderStickyNotes();
          toast('已撤销创建便签 (Ctrl+Z)');
        } else {
          const notes = loadStickyNotes();
          if (!notes.some(n => n.id === action.note.id)) {
            notes.push(action.note);
            saveStickyNotes(notes);
            renderStickyNotes();
          }
          toast('已重做创建便签 (Ctrl+Y)');
        }
        break;
      }
      case 'TIDY_ALL': {
        if (isUndo) {
          localStorage.setItem(SPATIAL_KEY_PREFIX + spatialScope(), JSON.stringify(action.oldLayout));
          renderForest();
          toast('已撤销智能布局，恢复自定义排版 (Ctrl+Z)');
        } else {
          clearAllSpatialLayout();
          renderForest();
          toast('已重做智能布局 (Ctrl+Y)');
        }
        break;
      }
      case 'BATCH_SPATIAL_MOVE': {
        const moves = action.moves || [];
        moves.forEach(m => {
          const pos = isUndo ? m.oldPos : m.newPos;
          saveSpatialNode(m.id, pos);
        });
        renderForest();
        toast(isUndo ? '已撤销批量移动 (Ctrl+Z)' : '已重做批量移动 (Ctrl+Y)');
        break;
      }
      case 'CARD_RENAME': {
        const title = isUndo ? action.oldTitle : action.newTitle;
        await persistTopology({ leaf_id: action.leafId, title });
        toast(isUndo ? '已撤销卡片重命名 (Ctrl+Z)' : '已重做卡片重命名 (Ctrl+Y)');
        break;
      }
      default: throw new Error('这项操作暂不支持撤销或重做');
    }
  }

  async function performHistoryAction(isUndo) {
    if (historyBusy) return false;
    const source = isUndo ? undoStack : redoStack;
    const target = isUndo ? redoStack : undoStack;
    if (!source.length) {
      toast(isUndo ? '没有可撤销的操作' : '没有可重做的操作');
      return false;
    }
    const action = source[source.length - 1], version = historyVersion;
    historyBusy = true;
    try {
      await applyAction(action, isUndo);
      const index = source.indexOf(action);
      if (index >= 0) source.splice(index, 1);
      // A new edit or workspace/clone barrier must never resurrect stale redo.
      if (index >= 0 && version === historyVersion) target.push(action);
      return true;
    } catch (error) {
      toast(`${isUndo ? '撤销' : '重做'}失败：${error.message}`, true);
      return false;
    } finally {
      historyBusy = false;
    }
  }

  function performUndo() { return performHistoryAction(true); }
  function performRedo() { return performHistoryAction(false); }

  // =========================================================================
  // §1.13 无限画布自由便签与多色随手记（Sticky Notes）
  // 便签层与画布共用 forestStage 坐标系；位置/尺寸/折叠态/内容实时写入 localStorage，
  // 不新增任何桥接或后端动作，刷新后按项目作用域恢复。
  // =========================================================================
  const STICKY_KEY_PREFIX = 'agentbus.stickyNotes:';
  const STICKY_COLORS = [
    { id: 'yellow', label: '明黄' },
    { id: 'green', label: '草绿' },
    { id: 'blue', label: '天空蓝' },
    { id: 'pink', label: '柔粉' },
    { id: 'purple', label: '淡紫' }
  ];
  const STICKY_DEFAULT_COLOR = 'yellow';
  const STICKY_DEFAULT_SIZE = { w: 240, h: 190 };
  const STICKY_PILL_SIZE = { w: 136, h: 40 };
  let lastCanvasPointer = null;
  let renderedStickyScope = '';

  function stickyColorIds() { return STICKY_COLORS.map(color => color.id); }
  function normalizeStickyColor(value) {
    const id = string(value).trim().toLowerCase();
    return stickyColorIds().includes(id) ? id : STICKY_DEFAULT_COLOR;
  }
  function stickyScope() { return spatialScope(); }
  function stickySize(note) {
    return note && note.collapsed ? STICKY_PILL_SIZE : { w: note.w, h: note.h };
  }
  function stickySourceNotes() {
    // §7.25 双通道（与 collapsed_branches 同策略）：
    //   主通道＝项目级宿主状态 snapshot.ui_state.notes（写在本机 ~/.agentbus_ui.json，重启后仍在）；
    //   兜底＝localStorage（离线运行或老版本缓存；private_mode 下重启即失效，故不能当唯一来源）。
    const hosted = snapshot.ui_state && snapshot.ui_state.notes;
    if (Array.isArray(hosted)) return hosted;
    try {
      const raw = localStorage.getItem(STICKY_KEY_PREFIX + stickyScope());
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch { return []; }
  }
  function loadStickyNotes() {
    try {
      return stickySourceNotes()
        .filter(note => note && typeof note === 'object' && string(note.id))
        .map(note => ({
          id: string(note.id),
          x: Number.isFinite(Number(note.x)) ? Number(note.x) : 40,
          y: Number.isFinite(Number(note.y)) ? Number(note.y) : 40,
          w: Number.isFinite(Number(note.w)) ? Math.max(120, Number(note.w)) : STICKY_DEFAULT_SIZE.w,
          h: Number.isFinite(Number(note.h)) ? Math.max(60, Number(note.h)) : STICKY_DEFAULT_SIZE.h,
          color: normalizeStickyColor(note.color),
          text: typeof note.text === 'string' ? note.text : '',
          collapsed: note.collapsed === true,
          attachedLeafId: typeof note.attachedLeafId === 'string' ? note.attachedLeafId : ''
        }));
    } catch { return []; }
  }
  function saveStickyNotes(notes) {
    // 双写：本地缓存立即生效（离线可用），项目级宿主状态负责跨重启保留。
    try { localStorage.setItem(STICKY_KEY_PREFIX + stickyScope(), JSON.stringify(notes)); } catch {}
    if (snapshot.ui_state) snapshot.ui_state.notes = notes;
    if (bridge() && online && stickyScope()) {
      void call('save_notes', { workspace: stickyScope(), notes })
        .catch(error => toast('便签未能保存到项目（重启后可能丢失）：' + error.message, true));
    }
  }
  function updateStickyNote(id, patch) {
    const notes = loadStickyNotes();
    const index = notes.findIndex(note => note.id === id);
    if (index < 0) return null;
    notes[index] = { ...notes[index], ...patch };
    saveStickyNotes(notes);
    return notes[index];
  }
  function removeStickyNote(id) {
    saveStickyNotes(loadStickyNotes().filter(note => note.id !== id));
  }
  function makeStickyId() {
    return 'note-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
  }
  function stickyContainer() {
    const stage = $('forestStage');
    if (!stage) return null;
    let host = $('forestStickies');
    if (!host) {
      host = el('div', 'forest-stickies');
      host.id = 'forestStickies';
      host.setAttribute('aria-label', '画布便签层');
      stage.appendChild(host);
    }
    return host;
  }
  function expandWorldForStickies() {
    const notes = loadStickyNotes();
    if (!notes.length) return;
    let maxX = 0, maxY = 0;
    notes.forEach(note => {
      const size = stickySize(note);
      maxX = Math.max(maxX, note.x + size.w);
      maxY = Math.max(maxY, note.y + size.h);
    });
    if (maxX + 100 > world.width || maxY + 100 > world.height) {
      world.width = Math.max(world.width, maxX + 200);
      world.height = Math.max(world.height, maxY + 200);
      const stage = $('forestStage');
      if (stage) { stage.style.width = world.width + 'px'; stage.style.height = world.height + 'px'; }
    }
  }
  function defaultStickyAnchor() {
    if (lastCanvasPointer) return { x: lastCanvasPointer.x, y: lastCanvasPointer.y };
    const viewport = $('forestViewport');
    if (viewport) {
      const bounds = viewport.getBoundingClientRect();
      return { x: Math.round((bounds.width / 2 - panX) / scale), y: Math.round((bounds.height / 2 - panY) / scale) };
    }
    return { x: 60, y: 60 };
  }
  function trackCanvasPointer(ev) {
    const viewport = $('forestViewport');
    if (!viewport) return;
    const bounds = viewport.getBoundingClientRect();
    if (ev.clientX < bounds.left || ev.clientX > bounds.right || ev.clientY < bounds.top || ev.clientY > bounds.bottom) return;
    lastCanvasPointer = { x: (ev.clientX - bounds.left - panX) / scale, y: (ev.clientY - bounds.top - panY) / scale };
  }
  function createStickyNote(x, y) {
    const anchor = (Number.isFinite(x) && Number.isFinite(y)) ? { x, y } : defaultStickyAnchor();
    const note = {
      id: makeStickyId(),
      x: Math.round(anchor.x - STICKY_DEFAULT_SIZE.w / 2),
      y: Math.round(anchor.y - 22),
      w: STICKY_DEFAULT_SIZE.w,
      h: STICKY_DEFAULT_SIZE.h,
      color: STICKY_DEFAULT_COLOR,
      text: '',
      collapsed: false
    };
    const notes = loadStickyNotes();
    notes.push(note);
    saveStickyNotes(notes);
    pushUndoAction({ type: 'STICKY_CREATE', id: note.id, note: { ...note } });
    const node = renderStickyNote(note);
    expandWorldForStickies();
    if (node) requestAnimationFrame(() => node.querySelector('.sticky-body')?.focus());
    toast('已贴上一张便签 · 随手记一笔');
    return note;
  }
  function renderStickyNote(note) {
    const host = stickyContainer();
    if (!host) return null;
    const size = stickySize(note);
    const node = el('div', 'sticky-note color-' + note.color + (note.collapsed ? ' collapsed' : ''));
    node.dataset.noteId = note.id;
    node.style.left = note.x + 'px';
    node.style.top = note.y + 'px';
    node.style.width = size.w + 'px';
    node.style.height = size.h + 'px';

    const head = el('div', 'sticky-head');
    head.title = '按住拖拽便签';
    const grip = el('span', 'sticky-grip', '⠿');
    grip.setAttribute('aria-hidden', 'true');
    const pillLabel = el('span', 'sticky-pill-label', '📝 便签');

    const tools = el('div', 'sticky-tools');
    const attachBtn = el('button', 'sticky-btn sticky-attach-btn', note.attachedLeafId ? '🔗' : '📎');
    attachBtn.type = 'button';
    attachBtn.title = note.attachedLeafId ? `已吸附至任务卡片 [${note.attachedLeafId}] (点击解除吸附)` : '吸附至最近任务卡片';
    attachBtn.setAttribute('aria-label', attachBtn.title);

    const convertBtn = el('button', 'sticky-btn sticky-convert-btn', '📋');
    convertBtn.type = 'button';
    convertBtn.title = '一键将便签转为正式任务卡片';
    convertBtn.setAttribute('aria-label', '转为任务卡片');

    const colorBtn = el('button', 'sticky-btn sticky-color-btn');
    colorBtn.type = 'button';
    colorBtn.title = '切换便签颜色（明黄 / 草绿 / 天空蓝 / 柔粉 / 淡紫）';
    colorBtn.setAttribute('aria-label', '切换便签颜色');
    colorBtn.appendChild(el('span', 'sticky-color-dot'));
    const collapseBtn = el('button', 'sticky-btn sticky-collapse-btn', note.collapsed ? '▢' : '–');
    collapseBtn.type = 'button';
    collapseBtn.title = note.collapsed ? '展开便签' : '折叠为小胶囊';
    collapseBtn.setAttribute('aria-label', collapseBtn.title);
    const deleteBtn = el('button', 'sticky-btn sticky-delete-btn', '×');
    deleteBtn.type = 'button';
    deleteBtn.title = '删除便签';
    deleteBtn.setAttribute('aria-label', '删除便签');
    append(tools, attachBtn, convertBtn, colorBtn, collapseBtn, deleteBtn);

    let attachedBadge = null;
    if (note.attachedLeafId) {
      node.classList.add('is-attached');
      attachedBadge = el('span', 'sticky-attached-badge', '🔗 锚定');
      append(head, grip, pillLabel, attachedBadge, tools);
    } else {
      append(head, grip, pillLabel, tools);
    }
    node.appendChild(head);

    const body = el('textarea', 'sticky-body');
    body.placeholder = '随手记 · 支持 Markdown 草稿与待办清单 (- [ ] 待办项)...';
    body.value = note.text;
    body.spellcheck = false;
    body.setAttribute('aria-label', '便签内容');
    node.appendChild(body);

    // §6.5 交互式 Markdown Checklist 原生打勾持久化
    const checklistBox = el('div', 'sticky-checklist');
    function updateChecklist() {
      checklistBox.replaceChildren();
      const lines = (note.text || '').split('\n');
      let count = 0;
      lines.forEach((line, idx) => {
        const m = line.match(/^\s*[-*]\s*\[([ xX])\]\s*(.*)$/);
        if (m) {
          count++;
          const checked = m[1].toLowerCase() === 'x';
          const label = el('label', 'sticky-checklist-item' + (checked ? ' checked' : ''));
          const chk = el('input');
          chk.type = 'checkbox';
          chk.checked = checked;
          chk.addEventListener('change', () => {
            const nextVal = chk.checked;
            const updated = [...lines];
            updated[idx] = line.replace(/\[([ xX])\]/, nextVal ? '[x]' : '[ ]');
            note.text = updated.join('\n');
            body.value = note.text;
            updateStickyNote(note.id, { text: note.text });
            updateChecklist();
            toast(nextVal ? '待办项已完成 ✓' : '待办项已重置为待办');
          });
          const textSpan = el('span', '', m[2]);
          append(label, chk, textSpan);
          checklistBox.appendChild(label);
        }
      });
      checklistBox.style.display = count > 0 && !note.collapsed ? 'block' : 'none';
    }
    updateChecklist();
    node.appendChild(checklistBox);

    body.addEventListener('input', () => {
      note.text = body.value;
      updateChecklist();
      updateStickyNote(note.id, { text: note.text });
    });

    const resize = el('span', 'sticky-resize');
    resize.setAttribute('aria-hidden', 'true');
    resize.title = '拖拽调整便签尺寸';
    node.appendChild(resize);

    host.appendChild(node);
    bindStickyInteractions(node, note, { head, body, colorBtn, collapseBtn, deleteBtn, resize, attachBtn, convertBtn, checklistBox, updateChecklist });
    return node;
  }
  function bindStickyInteractions(node, note, parts) {
    const { head, body, colorBtn, collapseBtn, deleteBtn, resize } = parts;
    const id = note.id;

    let startPointerX = 0, startPointerY = 0, noteStartX = note.x, noteStartY = note.y;
    let isMoving = false, currentX = note.x, currentY = note.y;
    head.addEventListener('pointerdown', ev => {
      if (ev.button !== 0 || ev.target.closest('button')) return;
      ev.stopPropagation();
      head.setPointerCapture(ev.pointerId);
      startPointerX = ev.clientX; startPointerY = ev.clientY;
      noteStartX = parseFloat(node.style.left) || note.x;
      noteStartY = parseFloat(node.style.top) || note.y;
      currentX = noteStartX; currentY = noteStartY; isMoving = false;
      function onMove(e) {
        const dx = (e.clientX - startPointerX) / scale;
        const dy = (e.clientY - startPointerY) / scale;
        if (!isMoving && Math.hypot(dx, dy) > 4) { isMoving = true; node.classList.add('is-spatial-dragging'); }
        if (isMoving) {
          currentX = Math.round(noteStartX + dx);
          currentY = Math.round(noteStartY + dy);
          node.style.left = currentX + 'px';
          node.style.top = currentY + 'px';
        }
      }
      function onUp(e) {
        head.releasePointerCapture(e.pointerId);
        head.removeEventListener('pointermove', onMove);
        head.removeEventListener('pointerup', onUp);
        head.removeEventListener('pointercancel', onUp);
        if (isMoving) {
          node.classList.remove('is-spatial-dragging');
          pushUndoAction({ type: 'STICKY_MOVE', id, oldPos: { x: noteStartX, y: noteStartY }, newPos: { x: currentX, y: currentY } });
          note.x = currentX; note.y = currentY;
          updateStickyNote(id, { x: currentX, y: currentY });
          expandWorldForStickies();
        }
      }
      head.addEventListener('pointermove', onMove);
      head.addEventListener('pointerup', onUp);
      head.addEventListener('pointercancel', onUp);
    });

    collapseBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      note.collapsed = !note.collapsed;
      const size = stickySize(note);
      node.classList.toggle('collapsed', note.collapsed);
      node.style.width = size.w + 'px';
      node.style.height = size.h + 'px';
      collapseBtn.textContent = note.collapsed ? '▢' : '–';
      collapseBtn.title = note.collapsed ? '展开便签' : '折叠为小胶囊';
      collapseBtn.setAttribute('aria-label', collapseBtn.title);
      updateStickyNote(id, { collapsed: note.collapsed });
      toast(note.collapsed ? '便签已折叠为小胶囊' : '便签已展开');
    });

    deleteBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      pushUndoAction({ type: 'STICKY_DELETE', note: { ...note } });
      removeStickyNote(id);
      node.remove();
      toast('便签已删除（按 Ctrl+Z 可撤销）');
    });

    colorBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      openStickyPalette(node, note);
    });

    let rzStartX = 0, rzStartY = 0, rzW = note.w, rzH = note.h;
    resize.addEventListener('pointerdown', ev => {
      if (ev.button !== 0) return;
      ev.stopPropagation();
      resize.setPointerCapture(ev.pointerId);
      rzStartX = ev.clientX; rzStartY = ev.clientY;
      rzW = parseFloat(node.style.width) || note.w;
      rzH = parseFloat(node.style.height) || note.h;
      node.classList.add('is-spatial-resizing');
      function onMove(e) {
        const w = Math.max(150, Math.round(rzW + (e.clientX - rzStartX) / scale));
        const h = Math.max(90, Math.round(rzH + (e.clientY - rzStartY) / scale));
        node.style.width = w + 'px';
        node.style.height = h + 'px';
        note.w = w; note.h = h;
      }
      function onUp(e) {
        resize.releasePointerCapture(e.pointerId);
        resize.removeEventListener('pointermove', onMove);
        resize.removeEventListener('pointerup', onUp);
        resize.removeEventListener('pointercancel', onUp);
        node.classList.remove('is-spatial-resizing');
        updateStickyNote(id, { w: note.w, h: note.h });
        expandWorldForStickies();
      }
      resize.addEventListener('pointermove', onMove);
      resize.addEventListener('pointerup', onUp);
      resize.addEventListener('pointercancel', onUp);
    });

    let textTimer = null;
    const persistText = () => { note.text = body.value; updateStickyNote(id, { text: body.value }); };
    body.addEventListener('input', () => { clearTimeout(textTimer); textTimer = setTimeout(persistText, 260); });
    body.addEventListener('blur', () => { clearTimeout(textTimer); persistText(); });

    // §6.5 便签一键转正式任务卡片
    const convertStickyToTask = async () => {
      const raw = (note.text || '').trim();
      if (!raw) return toast('便签内容为空，无法转为任务卡片', true);
      const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
      const title = lines[0].replace(/^#+\s*|^[-*]\s*(\[[ xX]\]\s*)?/i, '').trim();
      const goal = lines.slice(1).join('\n') || title;
      if (parts.convertBtn) parts.convertBtn.disabled = true;
      try {
        const res = await call('append_leaf', {
          parent_id: 'desktop',
          title: title || '新任务',
          goal: goal,
          dependencies: []
        });
        if (res && res.leaf) {
          saveSpatialNode(res.leaf.id, { x: note.x, y: note.y });
          removeStickyNote(note.id);
          node.remove();
          pushUndoAction({ type: 'STICKY_DELETE', note: { ...note } });
          toast(`便签已成功转为正式任务卡片 (§${res.leaf.section_no || res.leaf.id})！`);
          refresh();
        }
      } catch (err) {
        toast(`转任务失败: ${err.message}`, true);
        if (parts.convertBtn) parts.convertBtn.disabled = false;
      }
    };
    if (parts.convertBtn) parts.convertBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      convertStickyToTask();
    });

    // §6.5 便签吸附/解除吸附任务卡片
    if (parts.attachBtn) parts.attachBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      if (note.attachedLeafId) {
        note.attachedLeafId = '';
        updateStickyNote(note.id, { attachedLeafId: '' });
        node.classList.remove('is-attached');
        parts.attachBtn.textContent = '📎';
        parts.attachBtn.title = '吸附至最近任务卡片';
        head.querySelector('.sticky-attached-badge')?.remove();
        toast('已解除便签与卡片的吸附');
        return;
      }
      const cards = [...document.querySelectorAll('.leaf-card')];
      if (!cards.length) return toast('当前画布没有任务卡片可供吸附', true);
      let closestCard = null, minDist = Infinity;
      cards.forEach(c => {
        const cx = parseFloat(c.style.left) || 0;
        const cy = parseFloat(c.style.top) || 0;
        const dist = Math.hypot(cx - note.x, cy - note.y);
        if (dist < minDist) {
          minDist = dist;
          closestCard = c;
        }
      });
      if (closestCard && minDist < 450) {
        const targetId = closestCard.dataset.leafId;
        note.attachedLeafId = targetId;
        updateStickyNote(note.id, { attachedLeafId: targetId });
        node.classList.add('is-attached');
        parts.attachBtn.textContent = '🔗';
        parts.attachBtn.title = `已吸附至任务卡片 [${targetId}] (点击解除吸附)`;
        if (!head.querySelector('.sticky-attached-badge')) {
          const badge = el('span', 'sticky-attached-badge', '🔗 锚定');
          head.insertBefore(badge, parts.tools || head.querySelector('.sticky-tools'));
        }
        toast(`便签已成功锚定吸附至任务卡片 [${targetId}]！`);
      } else {
        toast('请将便签拖近任务卡片后再进行吸附', true);
      }
    });
  }
  function openStickyPalette(node, note) {
    document.querySelectorAll('.sticky-palette').forEach(palette => palette.remove());
    const palette = el('div', 'sticky-palette');
    palette.setAttribute('role', 'menu');
    STICKY_COLORS.forEach(color => {
      const swatch = el('button', 'sticky-swatch swatch-' + color.id + (color.id === note.color ? ' active' : ''));
      swatch.type = 'button';
      swatch.title = color.label;
      swatch.setAttribute('aria-label', '切换为' + color.label);
      swatch.addEventListener('click', ev => {
        ev.stopPropagation();
        applyStickyColor(node, note, color.id);
        palette.remove();
      });
      palette.appendChild(swatch);
    });
    node.appendChild(palette);
    requestAnimationFrame(() => {
      const close = ev => {
        if (!palette.contains(ev.target)) { palette.remove(); document.removeEventListener('pointerdown', close, true); }
      };
      document.addEventListener('pointerdown', close, true);
    });
  }
  function applyStickyColor(node, note, colorId) {
    const color = normalizeStickyColor(colorId);
    STICKY_COLORS.forEach(item => node.classList.remove('color-' + item.id));
    node.classList.add('color-' + color);
    note.color = color;
    updateStickyNote(note.id, { color });
    const label = (STICKY_COLORS.find(item => item.id === color) || {}).label || color;
    toast('便签配色：' + label);
  }
  function renderStickyNotes() {
    const host = stickyContainer();
    if (!host) return;
    host.replaceChildren();
    loadStickyNotes().forEach(note => renderStickyNote(note));
    renderedStickyScope = stickyScope();
  }
  function ensureStickyScope() {
    if (stickyScope() !== renderedStickyScope) renderStickyNotes();
  }
  function installStickyToolbarButton() {
    const bar = $('quickActionBar');
    if (!bar || $('quickStickyNoteBtn')) return;
    const btn = el('button', 'quick-btn sticky-quick-btn');
    btn.id = 'quickStickyNoteBtn';
    btn.type = 'button';
    btn.title = '在画布贴上随手记便签 (Alt+T)';
    btn.appendChild(el('span', 'sticky-quick-icon', '📝'));
    btn.appendChild(el('span', '', '便签'));
    btn.appendChild(el('kbd', '', 'Alt+T'));
    btn.addEventListener('click', () => createStickyNote());
    bar.appendChild(btn);
  }

  let showDependencyWires = false;
  try {
    showDependencyWires = localStorage.getItem('agentbus.showDepWires') === 'true';
    if (showDependencyWires) document.body.classList.add('show-dep-wires');
  } catch {}

  function toggleDependencyWires() {
    showDependencyWires = !showDependencyWires;
    try { localStorage.setItem('agentbus.showDepWires', String(showDependencyWires)); } catch {}
    document.body.classList.toggle('show-dep-wires', showDependencyWires);
    const btn = $('quickToggleWireBtn');
    if (btn) btn.classList.toggle('active', showDependencyWires);
    toast(showDependencyWires ? '已开启依赖关系虚线' : '已收起依赖虚线，保持白板纯净');
  }

  const PRIORITY_KEY_PREFIX = 'agentbus.priorities.';
  function cardPrioritySource() {
    // §7.30 双通道存储（同便签策略）：
    //   主通道＝项目级宿主状态 snapshot.ui_state.priorities（写在本机 ~/.agentbus_ui.json，重启/InPrivate仍保留）；
    //   兜底＝localStorage（离线/纯前端备选）。
    const hosted = snapshot.ui_state && snapshot.ui_state.priorities;
    if (hosted && typeof hosted === 'object') return hosted;
    try {
      const raw = localStorage.getItem(PRIORITY_KEY_PREFIX + spatialScope());
      return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
  }
  function loadCardPriorities() {
    try {
      const source = cardPrioritySource();
      const valid = {};
      for (const [k, v] of Object.entries(source)) {
        if (['P0', 'P1', 'P2'].includes(v)) valid[k] = v;
      }
      return valid;
    } catch { return {}; }
  }
  function saveCardPriorities(priorities) {
    try { localStorage.setItem(PRIORITY_KEY_PREFIX + spatialScope(), JSON.stringify(priorities)); } catch {}
    if (snapshot.ui_state) snapshot.ui_state.priorities = priorities;
    if (bridge() && online && spatialScope()) {
      void call('save_priorities', { workspace: spatialScope(), priorities })
        .catch(error => toast('卡片优先级未能保存到宿主（重启后可能丢失）：' + error.message, true));
    }
  }
  function cycleCardPriority(leafId) {
    const priorities = { ...loadCardPriorities() };
    const order = ['', 'P0', 'P1', 'P2'];
    const cur = priorities[leafId] || '';
    const next = order[(order.indexOf(cur) + 1) % order.length];
    if (next) priorities[leafId] = next;
    else delete priorities[leafId];
    saveCardPriorities(priorities);
    renderForest();
    toast(next ? `卡片已标记为个人视觉优先级 [${next}]（注：仅作看板视觉标记，不改变底层调度顺序）` : '已清除卡片个人视觉标记');
  }

  function updateCardConnectedEdges(leafId, newX, newY, cardW, cardH) {
    const branchEdge = document.getElementById('branch-edge-' + leafId);
    if (branchEdge) {
      const bx = parseFloat(branchEdge.dataset.branchX);
      const by = parseFloat(branchEdge.dataset.branchY);
      const ly = newY + cardH / 2;
      branchEdge.setAttribute('d', calcHorizontalBezier(bx, by, newX, ly));syncEdgeFlow(branchEdge);
    }
    [...document.querySelectorAll('path.dep-wire')].filter(wire => wire.dataset.depSource === leafId).forEach(wire => {
      const tgtId = wire.dataset.depTarget;
      const tgtCard = document.getElementById('leaf-node-' + tgtId);
      if (tgtCard) {
        const tx = parseFloat(tgtCard.style.left);
        const ty = parseFloat(tgtCard.style.top) + (tgtCard.offsetHeight || 88) / 2;
        wire.setAttribute('d', calcHorizontalBezier(newX + cardW, newY + cardH / 2, tx, ty));
      }
    });
    [...document.querySelectorAll('path.dep-wire')].filter(wire => wire.dataset.depTarget === leafId).forEach(wire => {
      const srcId = wire.dataset.depSource;
      const srcCard = document.getElementById('leaf-node-' + srcId);
      if (srcCard) {
        const sx = parseFloat(srcCard.style.left) + cardW;
        const sy = parseFloat(srcCard.style.top) + (srcCard.offsetHeight || 88) / 2;
        wire.setAttribute('d', calcHorizontalBezier(sx, sy, newX, newY + cardH / 2));
      }
    });
  }

  function updateBranchConnectedEdges(groupId, newX, newY, branchW, branchH, rootX, rootW, rootCenterY) {
    const trunkEdge = [...document.querySelectorAll('.forest-lines path[data-edge="trunk"]')].find(path => path.dataset.groupId === groupId);
    const by = newY + branchH / 2;
    if (trunkEdge) {
      const span = Math.max(30, (newX - (rootX + rootW)) * 0.5);
      trunkEdge.setAttribute('d', `M ${rootX + rootW} ${rootCenterY} C ${rootX + rootW + span} ${rootCenterY}, ${newX - span} ${by}, ${newX} ${by}`);syncEdgeFlow(trunkEdge);
    }
    [...document.querySelectorAll('.forest-lines path[data-collapsible-edge="true"]')].filter(wire => wire.dataset.groupId === groupId).forEach(wire => {
      const leafId = wire.dataset.leafId;
      const leafCard = document.getElementById('leaf-node-' + leafId);
      if (leafCard) {
        const lx = parseFloat(leafCard.style.left);
        const ly = parseFloat(leafCard.style.top) + (leafCard.offsetHeight || 88) / 2;
        const bx = newX + branchW;
        wire.setAttribute('d', calcHorizontalBezier(bx, by, lx, ly));
        wire.dataset.branchX = bx;
        wire.dataset.branchY = by;syncEdgeFlow(wire);
      }
    });
  }

  let branchDrag = null;
  let cardDrag = null;
  let wireDrag = null;
  let stickyDrag = null;

  function bindBranchSpatialDragging(branchNode, groupId, initX, initY, branchW, branchH, rootX, rootW, rootCenterY) {
    let startPointerX = 0, startPointerY = 0;
    let nodeStartX = initX, nodeStartY = initY;
    let isMoving = false;
    let currentX = initX, currentY = initY;

    branchNode.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      if (ev.target.closest('button, .branch-collapse-button, a, input, select')) return;
      ev.stopPropagation();
      ev.preventDefault();
      branchDrag = { active: true, groupId };
      branchNode.setPointerCapture(ev.pointerId);
      startPointerX = ev.clientX;
      startPointerY = ev.clientY;
      nodeStartX = parseFloat(branchNode.style.left) || initX;
      nodeStartY = parseFloat(branchNode.style.top) || initY;
      const startPos = loadSpatialLayout()[groupId] || null;
      currentX = nodeStartX;
      currentY = nodeStartY;
      isMoving = false;

      function onPointerMove(e) {
        const dx = (e.clientX - startPointerX) / scale;
        const dy = (e.clientY - startPointerY) / scale;
        if (!isMoving && Math.hypot(dx, dy) > 4) {
          isMoving = true;
          branchNode.classList.add('is-spatial-dragging');
        }
        if (isMoving) {
          currentX = Math.round(nodeStartX + dx);
          currentY = Math.round(nodeStartY + dy);
          branchNode.style.left = currentX + 'px';
          branchNode.style.top = currentY + 'px';
          updateBranchConnectedEdges(groupId, currentX, currentY, branchW, branchH, rootX, rootW, rootCenterY);
        }
      }

      function onPointerUp(e) {
        branchNode.releasePointerCapture(e.pointerId);
        branchNode.removeEventListener('pointermove', onPointerMove);
        branchNode.removeEventListener('pointerup', onPointerUp);
        branchNode.removeEventListener('pointercancel', onPointerUp);
        branchDrag = null;
        if (isMoving) {
          branchNode.classList.remove('is-spatial-dragging');
          pushUndoAction({ type: 'SPATIAL_MOVE', id: groupId, oldPos: startPos, newPos: { x: currentX, y: currentY } });
          saveSpatialNode(groupId, { x: currentX, y: currentY });
          if (currentX + branchW + 100 > world.width || currentY + branchH + 100 > world.height) {
            world.width = Math.max(world.width, currentX + branchW + 200);
            world.height = Math.max(world.height, currentY + branchH + 200);
            $('forestStage').style.width = world.width + 'px';
            $('forestStage').style.height = world.height + 'px';
          }
          toast('分支框位置已记忆（按 Ctrl+Z 可撤销）');
        }
      }

      branchNode.addEventListener('pointermove', onPointerMove);
      branchNode.addEventListener('pointerup', onPointerUp);
      branchNode.addEventListener('pointercancel', onPointerUp);
    });
  }

  // =========================================================================
  // Section 6.2: 框选矩形、批量多选平移、就地重命名与卡片克隆引擎
  // =========================================================================
  const batchSelectedCardIds = new Set();

  function clearBatchSelection() {
    batchSelectedCardIds.clear();
    document.querySelectorAll('.leaf-card.is-batch-selected').forEach(c => c.classList.remove('is-batch-selected'));
  }

  function toggleCardBatchSelected(cardId) {
    const leafId = cardId;
    if (batchSelectedCardIds.has(leafId)) {
      batchSelectedCardIds.delete(leafId);
      [...document.querySelectorAll('.leaf-card')].filter(c => c.dataset.leafId === leafId).forEach(c => c.classList.remove('is-batch-selected'));
    } else {
      batchSelectedCardIds.add(leafId);
      [...document.querySelectorAll('.leaf-card')].filter(c => c.dataset.leafId === leafId).forEach(c => c.classList.add('is-batch-selected'));
    }
  }

  function startInPlaceCardRename(card, leaf) {
    const titleSpan = card.querySelector('.leaf-title');
    if (!titleSpan || card.querySelector('.inplace-rename-input')) return;
    const oldTitle = leaf.title || '';
    const input = el('input', 'inplace-rename-input');
    input.type = 'text';
    input.value = oldTitle;
    titleSpan.style.display = 'none';
    titleSpan.parentNode.insertBefore(input, titleSpan.nextSibling);
    input.focus();
    input.select();

    let committed = false;
    async function commit(save) {
      if (committed) return;
      committed = true;
      const newTitle = input.value.trim();
      input.remove();
      titleSpan.style.display = '';
      if (save && newTitle && newTitle !== oldTitle) {
        try {
          await persistTopology({ leaf_id: leaf.id, title: newTitle });
          pushUndoAction({ type: 'CARD_RENAME', leafId: leaf.id, oldTitle, newTitle });
          toast(`已就地重命名为: ${newTitle} (Ctrl+Z 可撤销)`);
        } catch (err) {
          toast(`重命名失败: ${err.message}`, true);
        }
      }
    }

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        commit(true);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        commit(false);
      }
    });
    input.addEventListener('blur', () => commit(true));
  }

  let clipboardCard = null;

  async function executeCloneCard(sourceLeaf) {
    if (!sourceLeaf) return;
    const cloneTitle = '克隆 - ' + (sourceLeaf.title || '新任务');
    try {
      const res = await call('append_leaf', {
        title: cloneTitle,
        goal: sourceLeaf.goal || cloneTitle,
        parent_id: sourceLeaf.parent_id || 'desktop',
        dependencies: sourceLeaf.dependencies || []
      });
      if (res && res.leaf) {
        const oldLayout = loadSpatialLayout()[sourceLeaf.id];
        if (oldLayout) {
          saveSpatialNode(res.leaf.id, { x: oldLayout.x + 40, y: oldLayout.y + 40 });
        }
        clearHistory();
        toast(`已克隆生成卡片: ${cloneTitle}；克隆暂不支持撤销，之前的撤销历史已清空。`);
        refresh();
      }
    } catch (err) {
      toast(`克隆失败: ${err.message}`, true);
    }
  }

  function startMarqueeSelection(event) {
    const stage = $('forestStage');
    const viewport = $('forestViewport');
    if (!stage || !viewport) return;
    const vpBounds = viewport.getBoundingClientRect();
    const startStageX = (event.clientX - vpBounds.left - panX) / scale;
    const startStageY = (event.clientY - vpBounds.top - panY) / scale;

    const marquee = el('div', 'selection-marquee');
    marquee.style.left = startStageX + 'px';
    marquee.style.top = startStageY + 'px';
    marquee.style.width = '0px';
    marquee.style.height = '0px';
    stage.appendChild(marquee);

    viewport.setPointerCapture(event.pointerId);

    function onMarqueeMove(e) {
      const curStageX = (e.clientX - vpBounds.left - panX) / scale;
      const curStageY = (e.clientY - vpBounds.top - panY) / scale;
      const minX = Math.min(startStageX, curStageX);
      const minY = Math.min(startStageY, curStageY);
      const w = Math.abs(curStageX - startStageX);
      const h = Math.abs(curStageY - startStageY);

      marquee.style.left = minX + 'px';
      marquee.style.top = minY + 'px';
      marquee.style.width = w + 'px';
      marquee.style.height = h + 'px';

      document.querySelectorAll('.leaf-card').forEach(card => {
        const id = card.dataset.leafId;
        if (!id) return;
        const cx = parseFloat(card.style.left) || 0;
        const cy = parseFloat(card.style.top) || 0;
        const cw = parseFloat(card.style.width) || card.offsetWidth || 140;
        const ch = parseFloat(card.style.height) || card.offsetHeight || 60;
        const intersect = !(cx + cw < minX || cx > minX + w || cy + ch < minY || cy > minY + h);
        if (intersect) {
          batchSelectedCardIds.add(id);
          card.classList.add('is-batch-selected');
        } else if (!e.shiftKey) {
          batchSelectedCardIds.delete(id);
          card.classList.remove('is-batch-selected');
        }
      });
    }

    function onMarqueeUp(e) {
      if (viewport.hasPointerCapture(event.pointerId)) {
        viewport.releasePointerCapture(event.pointerId);
      }
      viewport.removeEventListener('pointermove', onMarqueeMove);
      viewport.removeEventListener('pointerup', onMarqueeUp);
      viewport.removeEventListener('pointercancel', onMarqueeUp);
      marquee.remove();
      if (batchSelectedCardIds.size > 0) {
        toast(`已框选 ${batchSelectedCardIds.size} 张任务卡，拖拽可批量平移`);
      }
    }

    viewport.addEventListener('pointermove', onMarqueeMove);
    viewport.addEventListener('pointerup', onMarqueeUp);
    viewport.addEventListener('pointercancel', onMarqueeUp);
  }

  function bindCardSpatialDragging(card, leaf, initX, initY, cardW, cardH, branchEdgeX, branchEdgeY) {
    let startPointerX = 0, startPointerY = 0;
    let cardStartX = initX, cardStartY = initY;
    let isMoving = false;
    let currentX = initX, currentY = initY;
    let isBatch = false;
    const batchInitPos = new Map();

    card.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      if (ev.target.closest('.anchor-port, .leaf-branch-badge, button:not(.leaf-card)')) return;
      if (ev.shiftKey) {
        ev.stopPropagation();
        ev.preventDefault();
        toggleCardBatchSelected(leaf.id);
        return;
      }
      ev.stopPropagation();
      cardDrag = { active: true, leafId: leaf.id };
      card.setPointerCapture(ev.pointerId);
      startPointerX = ev.clientX;
      startPointerY = ev.clientY;
      cardStartX = parseFloat(card.style.left) || initX;
      cardStartY = parseFloat(card.style.top) || initY;
      const startPos = loadSpatialLayout()[leaf.id] || null;
      currentX = cardStartX;
      currentY = cardStartY;
      isMoving = false;

      isBatch = batchSelectedCardIds.has(leaf.id) && batchSelectedCardIds.size > 1;
      batchInitPos.clear();
      if (isBatch) {
        batchSelectedCardIds.forEach(id => {
          const el = [...document.querySelectorAll('.leaf-card')].find(c => c.dataset.leafId === id);
          if (el) {
            batchInitPos.set(id, {
              el,
              startX: parseFloat(el.style.left) || 0,
              startY: parseFloat(el.style.top) || 0,
              startLayout: loadSpatialLayout()[id] || null
            });
          }
        });
      }

      function onPointerMove(e) {
        const dx = (e.clientX - startPointerX) / scale;
        const dy = (e.clientY - startPointerY) / scale;
        if (!isMoving && Math.hypot(dx, dy) > 4) {
          isMoving = true;
          card.classList.add('is-spatial-dragging');
          if (isBatch) {
            batchInitPos.forEach(info => info.el.classList.add('is-spatial-dragging'));
          }
        }
        if (isMoving) {
          if (isBatch) {
            batchInitPos.forEach((info, id) => {
              const nx = Math.round(info.startX + dx);
              const ny = Math.round(info.startY + dy);
              info.el.style.left = nx + 'px';
              info.el.style.top = ny + 'px';
              updateCardConnectedEdges(id, nx, ny, cardW, cardH);
            });
          } else {
            currentX = Math.round(cardStartX + dx);
            currentY = Math.round(cardStartY + dy);
            card.style.left = currentX + 'px';
            card.style.top = currentY + 'px';
            updateCardConnectedEdges(leaf.id, currentX, currentY, cardW, cardH);
          }
          // §6.5 同步平移吸附在当前卡片上的便签
          const attachedStickies = loadStickyNotes().filter(n => n.attachedLeafId === leaf.id);
          attachedStickies.forEach(sn => {
            const snNode = Array.from(document.querySelectorAll('.sticky-note')).find(n => n.dataset.noteId === sn.id);
            if (snNode) {
              snNode.style.left = Math.round(sn.x + (currentX - cardStartX)) + 'px';
              snNode.style.top = Math.round(sn.y + (currentY - cardStartY)) + 'px';
            }
          });
        }
      }

      function onPointerUp(e) {
        card.releasePointerCapture(e.pointerId);
        card.removeEventListener('pointermove', onPointerMove);
        card.removeEventListener('pointerup', onPointerUp);
        card.removeEventListener('pointercancel', onPointerUp);
        cardDrag = null;
        if (isMoving) {
          card.classList.remove('is-spatial-dragging');
          // 保存吸附便签新坐标
          const attachedStickies = loadStickyNotes().filter(n => n.attachedLeafId === leaf.id);
          attachedStickies.forEach(sn => {
            const nx = Math.round(sn.x + (currentX - cardStartX));
            const ny = Math.round(sn.y + (currentY - cardStartY));
            sn.x = nx; sn.y = ny;
            updateStickyNote(sn.id, { x: nx, y: ny });
          });
          if (isBatch) {
            batchInitPos.forEach(info => info.el.classList.remove('is-spatial-dragging'));
            const moves = [];
            batchInitPos.forEach((info, id) => {
              const nx = parseFloat(info.el.style.left) || 0;
              const ny = parseFloat(info.el.style.top) || 0;
              moves.push({ id, oldPos: info.startLayout, newPos: { x: nx, y: ny } });
              saveSpatialNode(id, { x: nx, y: ny });
            });
            pushUndoAction({ type: 'BATCH_SPATIAL_MOVE', moves });
            toast(`已批量平移 ${moves.length} 张任务卡（按 Ctrl+Z 可撤销）`);
          } else {
            pushUndoAction({ type: 'SPATIAL_MOVE', id: leaf.id, oldPos: startPos, newPos: { x: currentX, y: currentY } });
            saveSpatialNode(leaf.id, { x: currentX, y: currentY });
            toast('任务位置已记忆（按 Ctrl+Z 可撤销）');
          }
          suppressCardClick = true;
          setTimeout(() => { suppressCardClick = false; }, 80);
          if (currentX + cardW + 100 > world.width || currentY + cardH + 100 > world.height) {
            world.width = Math.max(world.width, currentX + cardW + 200);
            world.height = Math.max(world.height, currentY + cardH + 200);
            $('forestStage').style.width = world.width + 'px';
            $('forestStage').style.height = world.height + 'px';
          }
        }
      }

      card.addEventListener('pointermove', onPointerMove);
      card.addEventListener('pointerup', onPointerUp);
      card.addEventListener('pointercancel', onPointerUp);
    });
  }

  function bindPortWiring(portOut, leaf, cardW, cardH) {
    portOut.addEventListener('pointerdown', (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      wireDrag = { active: true, leafId: leaf.id };
      const stage = $('forestStage');
      const linesSvg = $('forestLines');
      const viewport = $('forestViewport');
      const bounds = viewport.getBoundingClientRect();
      stage.classList.add('is-wiring-active');
      portOut.setPointerCapture(ev.pointerId);

      const card = portOut.closest('.leaf-card');
      card.classList.add('is-wire-source');

      function wouldCreateCycle(srcId, dstId) {
        const visited = new Set();
        function reach(curr) {
          if (curr === srcId) return true;
          if (visited.has(curr)) return false;
          visited.add(curr);
          const leafObj = snapshot.forest.leaves.find(l => l.id === curr);
          if (!leafObj) return false;
          for (const dep of (leafObj.dependencies || [])) {
            if (reach(dep)) return true;
          }
          return false;
        }
        return reach(dstId);
      }

      const candidateCards = [];
      const invalidCards = [];
      document.querySelectorAll('.leaf-card').forEach(otherCard => {
        const otherId = otherCard.dataset.leafId;
        if (!otherId || otherId === leaf.id) {
          otherCard.classList.add('is-wire-invalid-candidate');
          invalidCards.push(otherCard);
          return;
        }
        const otherLeaf = snapshot.forest.leaves.find(l => l.id === otherId);
        const alreadyDep = (otherLeaf?.dependencies || []).includes(leaf.id);
        if (alreadyDep || wouldCreateCycle(leaf.id, otherId)) {
          otherCard.classList.add('is-wire-invalid-candidate');
          invalidCards.push(otherCard);
        } else {
          otherCard.classList.add('is-wire-target-candidate');
          candidateCards.push(otherCard);
        }
      });

      const srcX = parseFloat(card.style.left) + cardW;
      const srcY = parseFloat(card.style.top) + cardH / 2;

      const rubberband = document.createElementNS(NS, 'path');
      rubberband.setAttribute('class', 'rubberband-wire');
      rubberband.setAttribute('d', `M ${srcX} ${srcY} L ${srcX} ${srcY}`);
      linesSvg.appendChild(rubberband);

      function onWireMove(e) {
        const curStageX = (e.clientX - bounds.left - panX) / scale;
        const curStageY = (e.clientY - bounds.top - panY) / scale;
        rubberband.setAttribute('d', calcHorizontalBezier(srcX, srcY, curStageX, curStageY));

        const hitEl = document.elementFromPoint(e.clientX, e.clientY);
        const hitCard = hitEl?.closest('.leaf-card.is-wire-target-candidate');
        candidateCards.forEach(c => c.classList.toggle('is-wire-hover-target', c === hitCard));
      }

      async function onWireUp(e) {
        portOut.releasePointerCapture(e.pointerId);
        portOut.removeEventListener('pointermove', onWireMove);
        portOut.removeEventListener('pointerup', onWireUp);
        portOut.removeEventListener('pointercancel', onWireUp);
        wireDrag = null;
        rubberband.remove();
        stage.classList.remove('is-wiring-active');
        card.classList.remove('is-wire-source');
        candidateCards.forEach(c => c.classList.remove('is-wire-target-candidate', 'is-wire-hover-target'));
        invalidCards.forEach(c => c.classList.remove('is-wire-invalid-candidate'));

        const dropEl = document.elementFromPoint(e.clientX, e.clientY);
        const targetCard = dropEl?.closest('.leaf-card');
        if (!targetCard) return;
        const targetId = targetCard.dataset.leafId;
        if (!targetId || targetId === leaf.id) return;

        const targetLeaf = snapshot.forest.leaves.find(l => l.id === targetId);
        if (!targetLeaf) return;

        const currentDeps = Array.isArray(targetLeaf.dependencies) ? [...targetLeaf.dependencies] : [];
        if (currentDeps.includes(leaf.id)) {
          return toast('依赖关系已存在');
        }
        currentDeps.push(leaf.id);
        try {
          await persistTopology({
            leaf_id: targetId,
            dependencies: currentDeps
          });
          toast(`已建立前置依赖：${leaf.title} ➔ ${targetLeaf.title}`);
        } catch (err) {
          toast(`连接未确认：${err.message}`, true);
        }
      }

      portOut.addEventListener('pointermove', onWireMove);
      portOut.addEventListener('pointerup', onWireUp);
      portOut.addEventListener('pointercancel', onWireUp);
    });
  }

  function showBranchSwitchPopover(leaf, anchorEl) {
    document.querySelectorAll('.in-place-create-card, .wire-disconnect-popover').forEach(e => e.remove());
    const pop = el('div', 'in-place-create-card');
    pop.style.width = '240px';
    const rect = anchorEl.getBoundingClientRect();
    const bounds = $('forestViewport').getBoundingClientRect();
    pop.style.left = ((rect.left - bounds.left - panX) / scale) + 'px';
    pop.style.top = ((rect.bottom + 6 - bounds.top - panY) / scale) + 'px';

    const h = el('h4', '', '更改所属分支组');
    const closeBtn = el('button', 'icon-button mini', '×');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', () => pop.remove());
    h.appendChild(closeBtn);
    pop.appendChild(h);

    const select = el('select', 'select');
    const branches = snapshot.forest.branches.length ? snapshot.forest.branches : [
      { id: 'desktop', title: '桌面 UI 看板树' },
      { id: 'skills', title: '技能与计划树' },
      { id: 'reliability', title: '可靠性与证据树' },
      { id: 'members', title: '成员与记忆树' },
      { id: 'release', title: '集成打包树' }
    ];
    branches.forEach(b => {
      const opt = el('option', '', (b.section_no ? '§' + b.section_no + ' ' : '') + (b.title || b.id));
      opt.value = b.id;
      if (b.id === leaf.parent_id) opt.selected = true;
      select.appendChild(opt);
    });
    pop.appendChild(select);

    const actions = el('div', 'in-place-actions');
    const confirmBtn = el('button', 'button small primary', '确认切换');
    confirmBtn.type = 'button';
    confirmBtn.addEventListener('click', async () => {
      const newParent = select.value;
      confirmBtn.disabled = true;
      try {
        await persistTopology({ leaf_id: leaf.id, parent_id: newParent });
        pop.remove();
        toast(`已归入分支：${newParent}`);
      } catch (err) {
        confirmBtn.disabled = false;
        toast(`切换未确认：${err.message}`, true);
      }
    });
    actions.appendChild(confirmBtn);
    pop.appendChild(actions);
    pop.style.zIndex = '150';
    $('forestStage').appendChild(pop);
  }

  function showDisconnectPopover(depSourceId, targetLeaf, clickClientX, clickClientY) {
    document.querySelectorAll('.in-place-create-card, .wire-disconnect-popover').forEach(e => e.remove());
    const pop = el('div', 'wire-disconnect-popover');
    const bounds = $('forestViewport').getBoundingClientRect();
    pop.style.left = ((clickClientX - bounds.left - panX) / scale) + 'px';
    pop.style.top = ((clickClientY - bounds.top - panY) / scale) + 'px';

    const text = el('span', '', `断开依赖 ${depSourceId}？`);
    const delBtn = el('button', 'danger', '断开');
    delBtn.type = 'button';
    const cancelBtn = el('button', '', '取消');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', () => pop.remove());

    delBtn.addEventListener('click', async () => {
      delBtn.disabled = true;
      const currentDeps = (targetLeaf.dependencies || []).filter(d => d !== depSourceId);
      try {
        await persistTopology({
          leaf_id: targetLeaf.id,
          dependencies: currentDeps
        });
        pop.remove();
        toast('已断开该前序依赖');
      } catch (err) {
        delBtn.disabled = false;
        toast(`断开未确认：${err.message}`, true);
      }
    });

    append(pop, text, delBtn, cancelBtn);
    $('forestNodes').appendChild(pop);
  }

  function handleCanvasDblClick(ev) {
    if (ev.target.closest('.forest-node, button, input, .in-place-create-card, .wire-disconnect-popover, .preview-notice, .canvas-controls')) return;
    document.querySelectorAll('.in-place-create-card, .wire-disconnect-popover').forEach(e => e.remove());
    const viewport = $('forestViewport');
    const bounds = viewport.getBoundingClientRect();
    const stageX = Math.round((ev.clientX - bounds.left - panX) / scale);
    const stageY = Math.round((ev.clientY - bounds.top - panY) / scale);

    const pop = el('div', 'in-place-create-card');
    pop.style.left = stageX + 'px';
    pop.style.top = stageY + 'px';

    const h = el('h4', '', '就地新建任务');
    const closeBtn = el('button', 'icon-button mini', '×');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', () => pop.remove());
    h.appendChild(closeBtn);
    pop.appendChild(h);

    const titleInput = el('input', 'input');
    titleInput.placeholder = '任务标题，如：优化某模块...';
    titleInput.required = true;
    pop.appendChild(titleInput);

    const goalArea = el('textarea', 'textarea');
    goalArea.placeholder = '目标与交付范围...';
    goalArea.rows = 2;
    pop.appendChild(goalArea);

    const branchSelect = el('select', 'select');
    const branches = snapshot.forest.branches.length ? snapshot.forest.branches : [
      { id: 'desktop', title: '桌面 UI 看板树' },
      { id: 'skills', title: '技能与计划树' },
      { id: 'reliability', title: '可靠性与证据树' },
      { id: 'members', title: '成员与记忆树' },
      { id: 'release', title: '集成打包树' }
    ];
    branches.forEach(b => {
      const opt = el('option', '', (b.section_no ? '§' + b.section_no + ' ' : '') + (b.title || b.id));
      opt.value = b.id;
      branchSelect.appendChild(opt);
    });
    pop.appendChild(branchSelect);

    const actions = el('div', 'in-place-actions');
    const cancelBtn = el('button', 'button small', '取消');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', () => pop.remove());
    const createBtn = el('button', 'button small primary', '就地创建');
    createBtn.type = 'button';

    async function doCreate() {
      const title = titleInput.value.trim();
      if (!title) return toast('请输入任务标题', true);
      createBtn.disabled = true;
      try {
        const res = await call('append_leaf', {
          title,
          goal: goalArea.value.trim() || title,
          parent_id: branchSelect.value
        });
        const newId = res?.leaf?.id || ('TASK-' + Date.now().toString(36).toUpperCase());
        saveSpatialNode(newId, { x: stageX, y: stageY });
        pop.remove();
        const generation = mutationGeneration;
        setSnapshot(await call('snapshot', {}), generation);
        toast('任务已在当前位置创建！');
      } catch (err) {
        toast(err.message, true);
        createBtn.disabled = false;
      }
    }

    createBtn.addEventListener('click', doCreate);
    titleInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); doCreate(); }
      else if (e.key === 'Escape') pop.remove();
    });

    const stickyBtn = el('button', 'button small sticky-inplace-btn', '📝 便签');
    stickyBtn.type = 'button';
    stickyBtn.title = '在此处贴一张随手记便签';
    stickyBtn.addEventListener('click', () => { pop.remove(); createStickyNote(stageX, stageY); });

    actions.appendChild(cancelBtn);
    actions.appendChild(stickyBtn);
    actions.appendChild(createBtn);
    pop.appendChild(actions);
    pop.style.zIndex = '150';
    $('forestStage').appendChild(pop);
    requestAnimationFrame(() => titleInput.focus());
  }

  function updateAllRenderedEdges() {
    const stage = $('forestStage');
    if (!stage) return;
    const stageRect = stage.getBoundingClientRect();
    document.querySelectorAll('.leaf-card').forEach(card => {
      const id = card.dataset.id;
      if (!id) return;
      const rect = card.getBoundingClientRect();
      const currentX = (rect.left - stageRect.left) / scale;
      const currentY = (rect.top - stageRect.top) / scale;
      const cardW = parseFloat(card.dataset.width) || 320;
      const cardH = parseFloat(card.dataset.height) || (card.offsetHeight || 88);
      updateCardConnectedEdges(id, currentX, currentY, cardW, cardH);
    });
  }

  function handleTidyUp() {
    // 1. 抓取当前所有节点的屏幕坐标快照 (First)
    const oldPositions = new Map();
    document.querySelectorAll('.leaf-card, .branch-node').forEach(el => {
      const id = el.dataset.id || el.dataset.group;
      if (id) {
        const rect = el.getBoundingClientRect();
        oldPositions.set(id, { left: rect.left, top: rect.top });
      }
    });

    const oldLayout = { ...loadSpatialLayout() };
    if (Object.keys(oldLayout).length > 0) {
      pushUndoAction({ type: 'TIDY_ALL', oldLayout });
    }

    clearAllSpatialLayout();
    renderForest();

    // 2. 识别新坐标，计算反向偏移 (Last & Invert)
    const movingElements = [];
    document.querySelectorAll('.leaf-card, .branch-node').forEach(el => {
      const id = el.dataset.id || el.dataset.group;
      if (id && oldPositions.has(id)) {
        const oldPos = oldPositions.get(id);
        const newRect = el.getBoundingClientRect();
        const dx = oldPos.left - newRect.left;
        const dy = oldPos.top - newRect.top;
        if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
          el.style.transform = `translate(${dx}px, ${dy}px)`;
          el.style.transition = 'none';
          movingElements.push(el);
        }
      }
    });

    if (movingElements.length > 0 && !reducedMotion()) {
      // 3. 强制重绘，播放 350ms 弹簧飞行动画 (Play)
      requestAnimationFrame(() => {
        const stage = $('forestStage');
        if (stage) stage.classList.add('is-smart-layout-animating');
        movingElements.forEach(el => {
          el.style.transition = 'transform 360ms cubic-bezier(0.16, 1, 0.3, 1)';
          el.style.transform = 'translate(0px, 0px)';
        });

        // 连线在 360ms 飞行动画期间平滑高频重绘跟随
        const start = performance.now();
        function followEdges(now) {
          const elapsed = now - start;
          updateAllRenderedEdges();
          if (elapsed < 380) {
            requestAnimationFrame(followEdges);
          } else {
            movingElements.forEach(el => {
              el.style.transform = '';
              el.style.transition = '';
            });
            if (stage) stage.classList.remove('is-smart-layout-animating');
            updateAllRenderedEdges();
          }
        }
        requestAnimationFrame(followEdges);
      });
    }

    toast('✦ 已应用智能化紧凑布局与拓扑顺流 (Alt+A)');
  }


  let forestRenderKey = '';
  function renderForest(force = false) {
    // §7.27 数据未变化时避免全量重建活动 DOM (REFRESH-PRESERVE-EDIT)
    // A snapshot timestamp alone is not a render fingerprint: folding, workspace
    // changes, branch topology and receipt/detail fields can change while the
    // leaf status tuple stays identical. Fingerprint the actual render inputs.
    const currentForestKey = JSON.stringify({
      workspace: snapshot.project.workspace || snapshot.config.workspace || '',
      stopped: snapshot.forest.stopped,
      filter,
      searchQuery,
      focusedMember,
      selectedLeaf,
      collapsedBranches: [...collapsedBranches].sort(),
      roots: snapshot.forest.roots,
      branches: snapshot.forest.branches,
      leaves: snapshot.forest.leaves,
      members: snapshot.members,
      pendingGoals: snapshot.pending_goals
    });
    if (!force && currentForestKey === forestRenderKey && $('forestNodes').children.length > 0) {
      updateMinimap();
      return;
    }
    forestRenderKey = currentForestKey;
    // Replaced nodes cannot deliver mouseleave; never retain a detached hover lens.
    $('forestStage').classList.remove('has-hover');
    const nodes=$('forestNodes');nodes.replaceChildren();$('forestLines').replaceChildren();
    const leaves=snapshot.forest.leaves,root=snapshot.forest.roots[0];
    if(snapshot.project.configured!==true){updateFirstRunGuide();return;}
    const setup=$('firstRunSetup');if(setup)setup.hidden=true;
    $('canvasEmpty').hidden=Boolean(leaves.length||root||snapshot.pending_goals.length);
    if(!leaves.length&&!root&&!snapshot.pending_goals.length){updateFirstRunGuide();return;}
    const {bands,rootLayouts}=forestLayout();
    const branchById=new Map(snapshot.forest.branches.map(branch=>[branch.id,branch]));
    function branchChildren(id,fallback=[]) {
      if(!branchById.has(id))return fallback.map(leaf=>({kind:'leaf',node:leaf}));
      const children=[...leaves.filter(leaf=>leaf.parent_id===id).map(node=>({kind:'leaf',node})),
        ...snapshot.forest.branches.filter(branch=>branch.parent_id===id).map(node=>({kind:'branch',node}))];
      return children.sort((a,b)=>string(a.node.section_no||'').localeCompare(string(b.node.section_no||''),undefined,{numeric:true})||(a.kind==='leaf'?-1:1));
    }
    function branchDescendants(id,fallback=[],seen=new Set()) {
      if(seen.has(id))return [];
      if(!branchById.has(id))return fallback;
      const next=new Set(seen);next.add(id);
      return [...leaves.filter(leaf=>leaf.parent_id===id),...snapshot.forest.branches.filter(branch=>branch.parent_id===id).flatMap(branch=>branchDescendants(branch.id,[],next))];
    }

    // 自左向右 (Left-to-Right) 三列几何坐标基准 (Owner 指令标准规范)
    const rootX=40, rootW=320, rootH=74;
    const branchX=460, branchW=300, branchH=60;
    const leafX=860, leafW=320;
    const nestedColumnStep=branchW+100;

    function layoutBranchChildren(parentId,depth,ancestors,fallback=[],seen=new Set()) {
      if(seen.has(parentId))return {height:branchH,items:[]};
      const nextSeen=new Set(seen);nextSeen.add(parentId);
      const childList=branchChildren(parentId,fallback);
      if(!childList.length)return {height:branchH,items:[]};

      const baseX=leafX+Math.max(0,depth-1)*nestedColumnStep;
      const pureLeaves=childList.filter(item=>item.kind==='leaf');
      const subBranches=childList.filter(item=>item.kind==='branch');
      const items=[];

      let leavesHeight=0;

      // 智能化多列紧凑布局核心：当叶子任务超过 3 个时自动启用双列紧凑自适应流动网格与 DAG 拓扑顺流
      if(pureLeaves.length > 4 || pureLeaves.length > 3){
        const colGap=28;
        const colW=leafW;
        const colX0=baseX;
        const colX1=baseX+colW+colGap;

        // 严格 DAG 拓扑分层：计算每个任务在当前分支内的拓扑层级 (Topological Layering)
        const pureLeafMap=new Map(pureLeaves.map(l=>[l.node.id, l.node]));
        const leafLayers=new Map();
        function getLayer(id, visited=new Set()){
          if(leafLayers.has(id)) return leafLayers.get(id);
          if(visited.has(id)) return 0;
          visited.add(id);
          const leaf=pureLeafMap.get(id);
          if(!leaf) return 0;
          const localDeps=(leaf.dependencies||[]).filter(d=>pureLeafMap.has(d));
          if(!localDeps.length){ leafLayers.set(id, 0); return 0; }
          let maxD=0;
          for(const depId of localDeps){
            maxD=Math.max(maxD, getLayer(depId, visited)+1);
          }
          leafLayers.set(id, maxD);
          return maxD;
        }
        pureLeaves.forEach(l=>getLayer(l.node.id));

        // 按拓扑层级（前置依赖优先排在前列）与章节号排序，确保上游因果流在前
        const sortedLeaves=[...pureLeaves].sort((a,b)=>{
          const la=leafLayers.get(a.node.id)||0;
          const lb=leafLayers.get(b.node.id)||0;
          if(la!==lb) return la - lb;
          return string(a.node.section_no||'').localeCompare(string(b.node.section_no||''),undefined,{numeric:true});
        });

        const colCursors=[0, 0];
        for(const item of sortedLeaves){
          const height=leafCardHeight(item.node);
          const layer=leafLayers.get(item.node.id)||0;
          let targetCol=0;
          if(layer > 0 && colCursors[1] <= colCursors[0] + 160){
            // 下游依赖任务自然顺流入右列 (Col 1)，形成自左向右自然水流
            targetCol=1;
          }else{
            // 高度平衡贪心算法：放入当前更矮的一列
            targetCol=colCursors[0]<=colCursors[1]?0:1;
          }

          const cardX=targetCol===0?colX0:colX1;
          const cardY=colCursors[targetCol];
          items.push({
            kind:'leaf',
            node:item.node,
            x:cardX,
            y:cardY,
            height,
            descendants:[item.node],
            parentId,
            foldAncestors:[...ancestors]
          });
          colCursors[targetCol]+=height+14;
        }
        leavesHeight=Math.max(colCursors[0], colCursors[1]);
      }else{
        // 4 个及以下保持经典单列优雅排布
        let cursor=0;
        for(const item of pureLeaves){
          const height=leafCardHeight(item.node);
          items.push({
            kind:'leaf',
            node:item.node,
            x:baseX,
            y:cursor,
            height,
            descendants:[item.node],
            parentId,
            foldAncestors:[...ancestors]
          });
          cursor+=height+14;
        }
        leavesHeight=cursor;
      }

      // 如果有嵌套子分支，排列在叶子卡片下方顺延
      let branchCursor=leavesHeight;
      for(const item of subBranches){
        const childId=item.node.id,collapsed=collapsedBranches.has(childId);
        const nested=collapsed?{height:branchH,items:[]}:layoutBranchChildren(childId,depth+1,[...ancestors,childId],[],nextSeen);
        const slotHeight=Math.max(branchH,nested.height),nestedOffset=Math.round((slotHeight-branchH)/2);
        items.push({
          kind:'branch',
          node:item.node,
          x:baseX,
          y:branchCursor+Math.round((slotHeight-branchH)/2),
          height:branchH,
          descendants:branchDescendants(childId),
          parentId,
          foldAncestors:[...ancestors],
          collapsed
        });
        for(const nestedItem of nested.items)items.push({...nestedItem,y:nestedItem.y+branchCursor+nestedOffset});
        branchCursor+=slotHeight+14;
      }

      const totalH=Math.max(branchH, (branchCursor>0?branchCursor-14:0));
      return {height:totalH,items};
    }

    // 垂直流式几何布局排布
    let cursorY=40;
    const computedGroups=bands.map(group=>{
      const id=group.id, real=branchById.has(id);
      const descendants=real?branchDescendants(id):group.children;
      const collapsed=collapsedBranches.has(id);
      const layout=collapsed?{height:branchH,items:[]}:layoutBranchChildren(id,1,[id],real?[]:group.children);
      const bandH=Math.max(branchH,layout.height),by=cursorY+Math.round((bandH-branchH)/2);
      const childItems=layout.items.map(item=>({...item,y:item.y+cursorY}));
      cursorY+=bandH+28;
      return {group,id,descendants,collapsed,childItems,by,centerY:by+branchH/2,height:bandH};
    });

    const totalHeight=Math.max(480,cursorY-28);
    const rootCenterY=Math.max(80,Math.round(totalHeight/2));
    const rootY=Math.max(20,rootCenterY-Math.round(rootH/2));

    const nestedRight=computedGroups.flatMap(group=>group.childItems).reduce((right,item)=>Math.max(right,item.x+(item.kind==='branch'?branchW:leafW)),leafX+leafW);
    world={width:Math.max(1280,nestedRight+80+(snapshot.pending_goals.length?320:0)),height:Math.max(560,totalHeight+80)};
    $('forestStage').style.width=world.width+'px';$('forestStage').style.height=world.height+'px';

    // 1. 最左侧项目主干根节点 (Root Node)
    rootLayouts.forEach(({root,children})=>{
      const trunk=positioned(el('button','forest-node trunk-node'),rootX,rootY,rootW,rootH);
      trunk.type='button';trunk.dataset.rootId=root.id;trunk.title=string(root.title);trunk.setAttribute('aria-label','查看项目主干：'+root.title);
      const done=children.filter(leaf=>leaf.state==='delivered').length;
      append(trunk,append(el('div','trunk-kicker'),el('span','status-dot emerald'),el('span','',`项目主干${root?.section_no?' · '+root.section_no:''}`)),el('h2','',shortTitle(root?.title||projectName(),25)),append(el('div','trunk-meta'),el('span','',snapshot.forest.stopped?'派工已停止':'项目总目标'),el('strong','',done+' / '+children.length+' 项完成')));
      trunk.addEventListener('click',()=>showRoot(root));
      trunk.addEventListener('mouseenter',()=>{
        $('forestStage').classList.add('has-hover');
        trunk.classList.add('node-highlight');
        document.querySelectorAll('.forest-lines path[data-edge="trunk"]').forEach(p=>p.classList.add('path-highlight'));
      });
      trunk.addEventListener('mouseleave',()=>{
        $('forestStage').classList.remove('has-hover');
        document.querySelectorAll('.node-highlight').forEach(n=>n.classList.remove('node-highlight'));
        document.querySelectorAll('.path-highlight').forEach(p=>p.classList.remove('path-highlight'));
      });
      nodes.appendChild(trunk);
    });

    // 2. 根节点 -> 分支节点：纯水平平滑三次贝塞尔 S 曲线 (全程切线向右，0 个 90 度折弯)
    const spatialPositions = loadSpatialLayout();
    computedGroups.forEach(cg=>{
      const group=cg.group, groupLeaves=cg.descendants, state=edgeState(groupLeaves);
      const savedBranchPos = spatialPositions[cg.id];
      const curBranchX = savedBranchPos ? savedBranchPos.x : branchX;
      const curBranchY = savedBranchPos ? savedBranchPos.y : cg.by;
      const curBranchCenterY = curBranchY + branchH / 2;
      const span=Math.max(30, (curBranchX-(rootX+rootW))*0.5);
      const c1x=rootX+rootW+span, c1y=rootCenterY;
      const c2x=curBranchX-span, c2y=curBranchCenterY;
      const d=savedBranchPos ? calcHorizontalBezier(rootX+rootW, rootCenterY, curBranchX, curBranchCenterY) : `M ${rootX+rootW} ${rootCenterY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${branchX} ${cg.centerY}`;
      const edge=treeEdge(d,'trunk',groupLeaves,state);
      edge.dataset.groupId=group.id;
    });

    // 3. 中间列工作分支节点与右侧列下辖任务卡片
    computedGroups.forEach(cg=>{
      const group=cg.group, id=cg.id, nodeInfo=cg.group, real=branchById.has(id), descendants=cg.descendants, collapsed=cg.collapsed;
      const title=nodeInfo.title||'工作分支';
      const savedBranchPos = spatialPositions[cg.id];
      const curBranchX = savedBranchPos ? savedBranchPos.x : branchX;
      const curBranchY = savedBranchPos ? savedBranchPos.y : cg.by;
      const curBranchCenterY = curBranchY + branchH / 2;
      const branchNode=positioned(el('div','forest-node branch-node'),curBranchX,curBranchY,branchW,branchH);
      branchNode.id = 'branch-node-' + id;
      tagBranchNode(branchNode,group.id,id,[],group.root.id,descendants.map(leaf=>leaf.id));branchNode.dataset.activity=descendants.length&&descendants.every(leaf=>leaf.state==='delivered')?'completed':descendants.some(leaf=>leaf.state==='running')?'working':descendants.some(leaf=>leaf.state==='blocked'&&!leaf.unmet_dependencies?.length)?'blocked':'idle';branchNode.title=group.root.title+' · '+title;branchNode.setAttribute('aria-level','1');
      const done=descendants.filter(leaf=>leaf.state==='delivered').length,percent=descendants.length?Math.round(done/descendants.length*100):0,numbers=[...new Set(descendants.map(leaf=>taskNumber(leaf).split('.').slice(0,-1).join('.')).filter(Boolean))];
      const meta=append(el('div','branch-meta'),el('span','',descendants.length+' 项任务'),el('strong','',percent+'%')),summary=el('span','branch-collapse-summary','+ '+descendants.length+' 项 · '+percent+'% 完成');meta.hidden=collapsed;summary.hidden=!collapsed;
      const bHeader=el('div','branch-header');
      bHeader.appendChild(el('span','branch-badge',nodeInfo.section_no?'§ '+nodeInfo.section_no:numbers.length?'§ '+numbers.slice(0,3).join(' / '):'工作分支'));
      bHeader.appendChild(el('h3','branch-title',shortTitle(title,13)));
      append(branchNode,bHeader,meta,summary,el('span','node-anchor'));
      const toggle=el('button','branch-collapse-button'+(collapsed?' collapsed':''));
      toggle.type='button';
      toggle.setAttribute('aria-expanded',String(!collapsed));
      toggle.setAttribute('aria-label',(collapsed?'展开':'收起')+'分支 '+title);
      toggle.title=(collapsed?'展开':'收起')+'此分支中的任务';
      toggle.innerHTML=collapsed
        ?'<svg viewBox="0 0 16 16" class="fold-icon"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
        :'<svg viewBox="0 0 16 16" class="fold-icon"><path d="M3 8h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
      toggle.disabled=!descendants.length;
      toggle.addEventListener('click',event=>{
        event.preventDefault();
        event.stopPropagation?.();
        setBranchCollapsed(id,!collapsed);
      });
      branchNode.appendChild(toggle);
      const progress=el('div','branch-progress');progress.setAttribute('role','progressbar');progress.setAttribute('aria-label',title+' 正式交付 '+done+' / '+descendants.length);progress.setAttribute('aria-valuenow',String(percent));progress.setAttribute('aria-valuemin','0');progress.setAttribute('aria-valuemax','100');const fill=el('span');fill.style.width=percent+'%';progress.appendChild(fill);branchNode.appendChild(progress);
      
      // 分支卡片 hover 全链路探照高亮
      branchNode.addEventListener('mouseenter',()=>{
        $('forestStage').classList.add('has-hover');
        branchNode.classList.add('node-highlight');
        document.querySelector('.trunk-node')?.classList.add('node-highlight');
        [...document.querySelectorAll('.leaf-card')].filter(c => c.dataset.groupId === group.id).forEach(c => c.classList.add('node-highlight'));
        document.querySelectorAll('.forest-lines path').forEach(p=>{
          if(p.dataset.groupId===group.id)p.classList.add('path-highlight');
        });
      });
      branchNode.addEventListener('mouseleave',()=>{
        $('forestStage').classList.remove('has-hover');
        document.querySelectorAll('.node-highlight').forEach(n=>n.classList.remove('node-highlight'));
        document.querySelectorAll('.path-highlight').forEach(p=>p.classList.remove('path-highlight'));
      });

      // 绑定分支节点自由空间拖拽
      bindBranchSpatialDragging(branchNode, id, curBranchX, curBranchY, branchW, branchH, rootX, rootW, rootCenterY);

      nodes.appendChild(branchNode);

      if(collapsed)return;

      // 绘制右侧叶子任务卡片与分支->叶子的水平顺滑三次贝塞尔连线
      const renderedBranches=new Map([[id,{node:branchNode,x:curBranchX,y:curBranchY,width:branchW,height:branchH,title}]]);
      cg.childItems.forEach(child=>{
        if(child.kind==='branch'){
          const subBranch=child.node,parent=renderedBranches.get(child.parentId);
          if(!parent)return;
          const subTitle=subBranch.title||'子分支',subX=spatialPositions[subBranch.id]?.x??child.x,subY=spatialPositions[subBranch.id]?.y??child.y;
          const subNode=positioned(el('div','forest-node branch-node nested-branch'+(child.collapsed?' collapsed':'')),subX,subY,branchW,branchH);
          subNode.id='branch-node-'+subBranch.id;tagBranchNode(subNode,group.id,subBranch.id,child.foldAncestors||[id],group.root.id,child.descendants.map(leaf=>leaf.id));
          subNode.dataset.activity=child.descendants.length&&child.descendants.every(leaf=>leaf.state==='delivered')?'completed':child.descendants.some(leaf=>leaf.state==='running')?'working':child.descendants.some(leaf=>leaf.state==='blocked'&&!leaf.unmet_dependencies?.length)?'blocked':'idle';
          subNode.title=group.root.title+' · '+subTitle;subNode.setAttribute('aria-level',String((child.foldAncestors||[]).length+1));
          const subDone=child.descendants.filter(leaf=>leaf.state==='delivered').length,subPercent=child.descendants.length?Math.round(subDone/child.descendants.length*100):0;
          const subMeta=append(el('div','branch-meta'),el('span','',child.descendants.length+' 项任务'),el('strong','',subPercent+'%'));
          const subSummary=el('span','branch-collapse-summary','+ '+child.descendants.length+' 项 · '+subPercent+'% 完成');
          subMeta.hidden=child.collapsed;subSummary.hidden=!child.collapsed;
          const subHeader=el('div','branch-header');
          subHeader.appendChild(el('span','branch-badge','§ '+(subBranch.section_no||'')));
          subHeader.appendChild(el('h3','branch-title',shortTitle(subTitle,13)));
          append(subNode,subHeader,subMeta,subSummary,el('span','node-anchor'));
          const subToggle=el('button','branch-collapse-button'+(child.collapsed?' collapsed':''));subToggle.type='button';subToggle.setAttribute('aria-expanded',String(!child.collapsed));subToggle.setAttribute('aria-label',(child.collapsed?'展开':'收起')+'分支 '+subTitle);subToggle.title=(child.collapsed?'展开':'收起')+'此分支中的任务';
          subToggle.innerHTML=child.collapsed
            ?'<svg viewBox="0 0 16 16" class="fold-icon"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
            :'<svg viewBox="0 0 16 16" class="fold-icon"><path d="M3 8h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
          subToggle.disabled=!child.descendants.length;subToggle.addEventListener('click',event=>{event.preventDefault();event.stopPropagation?.();setBranchCollapsed(subBranch.id,!child.collapsed);});subNode.appendChild(subToggle);
          const subProgress=el('div','branch-progress');subProgress.setAttribute('role','progressbar');subProgress.setAttribute('aria-label',subTitle+' 正式交付 '+subDone+' / '+child.descendants.length);subProgress.setAttribute('aria-valuenow',String(subPercent));subProgress.setAttribute('aria-valuemin','0');subProgress.setAttribute('aria-valuemax','100');const subFill=el('span');subFill.style.width=subPercent+'%';subProgress.appendChild(subFill);subNode.appendChild(subProgress);
          subNode.addEventListener('mouseenter',()=>{ $('forestStage').classList.add('has-hover');subNode.classList.add('node-highlight');parent.node.classList.add('node-highlight');document.querySelector('.trunk-node')?.classList.add('node-highlight');document.querySelectorAll('.forest-lines path').forEach(path=>{if(path.dataset.groupId===group.id&&JSON.parse(path.dataset.leafIds||'[]').some(leafId=>child.descendants.some(leaf=>leaf.id===leafId)))path.classList.add('path-highlight');});});
          subNode.addEventListener('mouseleave',()=>{$('forestStage').classList.remove('has-hover');document.querySelectorAll('.node-highlight').forEach(node=>node.classList.remove('node-highlight'));document.querySelectorAll('.path-highlight').forEach(path=>path.classList.remove('path-highlight'));});
          bindBranchSpatialDragging(subNode,subBranch.id,subX,subY,branchW,branchH,rootX,rootW,rootCenterY);nodes.appendChild(subNode);
          renderedBranches.set(subBranch.id,{node:subNode,x:subX,y:subY,width:branchW,height:branchH,title:subTitle});
          const startX=parent.x+parent.width,startY=parent.y+parent.height/2,endY=subY+branchH/2;
          const path=treeEdge(calcHorizontalBezier(startX,startY,subX,endY),'branch',child.descendants,edgeState(child.descendants));tagCollapsibleEdge(path,group.id,child.foldAncestors||[id]);path.dataset.branchX=startX;path.dataset.branchY=startY;
          return;
        }
        const isLeaf=child.kind==='leaf',leaf=child.node,parent=renderedBranches.get(child.parentId)||renderedBranches.get(id);
        if(!parent)return;
        const nodeInfo=branchById.get(child.parentId)||group,branchNode=parent.node,curBranchX=parent.x,curBranchCenterY=parent.y+parent.height/2,cardX=child.x;
        if(isLeaf){
          const savedPos = spatialPositions[leaf.id];
          const curCardX = savedPos ? savedPos.x : cardX;
          const curCardY = savedPos ? savedPos.y : child.y;
          const visual=leaf.state==='blocked'&&leaf.unmet_dependencies?.length?'waiting':leaf.state,motion=visual==='waiting'?'':pendingTransitions.get(leaf.id)||'',card=positioned(el('button','forest-node leaf-card '+string(visual)+(motion?' '+motion:'')),curCardX,curCardY,leafW,child.height);
          if(leaf.artifact_state==='mismatch')card.classList.add('artifact-drift');
          const driftedDependencies=driftedPrerequisites(leaf);
          const hasDriftedDependency=driftedDependencies.length>0,activeWithDrift=hasDriftedDependency&&['claimed','running'].includes(leaf.state);
          if(hasDriftedDependency)card.classList.add('dependency-input-drift');
          if(motion==='result-arrived')card.addEventListener('animationend',event=>{
            if(event.target===card&&event.animationName==='result-arrive')card.classList.remove('result-arrived');
          });
          card.id = 'leaf-node-' + leaf.id;
          card.style.width=leafW+'px';card.style.height=child.height+'px';card.type='button';card.dataset.leafId=leaf.id;card.dataset.sectionNo=taskNumber(leaf);card.dataset.rootId=group.root.id;card.dataset.groupId=group.id;card.dataset.foldAncestors=JSON.stringify(child.foldAncestors||[id]);card.dataset.owner=leaf.owner||'';card.title=numberedTitle(leaf)+(leaf.blocked_reason?'\n'+leaf.blocked_reason:'');card.setAttribute('aria-label',numberedTitle(leaf)+'，'+taskStateLabel(leaf)+'，'+memberName(leaf.owner)+(hasDriftedDependency?'，前序产物待核对':''));
          const symbol=el('span','leaf-state-symbol');if(leaf.state==='delivered')symbol.appendChild(icon('check'));else symbol.textContent=leaf.state==='running'?'↗':leaf.state==='claimed'?'·':leaf.state==='awaiting_audit'?'◌':leaf.state==='rejected'?'×':visual==='blocked'?'!':'○';
          const leafTop=el('div','leaf-top');if(taskNumber(leaf))leafTop.appendChild(el('span','task-section',taskNumber(leaf)));append(leafTop,el('span','leaf-title',shortTitle(leaf.title,14)),symbol);
          
          // 微型所属分支切换胶囊 (点击切换 parent_id)
          const branchBadge = el('span', 'leaf-branch-badge', '§ ' + (nodeInfo.title || '分支'));
          branchBadge.title = '点击就地更换所属分支组 (parent_id)';
          branchBadge.addEventListener('click', (ev) => {
            ev.stopPropagation();
            ev.preventDefault();
            showBranchSwitchPopover(leaf, branchBadge);
          });
          leafTop.appendChild(branchBadge);

          // 笔记化优先级药丸 (点击循环切换 P0/P1/P2/无)
          const prioMap = loadCardPriorities();
          const prio = prioMap[leaf.id] || '';
          const prioBadge = el('span', 'leaf-priority-badge ' + (prio ? 'prio-' + prio.toLowerCase() : 'prio-none'), prio || '☆');
          prioBadge.title = '个人视觉优先级标记 (☆普通 -> P0极速 -> P1核心 -> P2稳步)\n注：仅为个人看板视觉标注，不作为调度执行器的算法排序依据';
          prioBadge.addEventListener('click', (ev) => {
            ev.stopPropagation();
            ev.preventDefault();
            cycleCardPriority(leaf.id);
          });
          leafTop.appendChild(prioBadge);
          if (prio) card.classList.add('priority-' + prio.toLowerCase());

          // §3.8 会话心跳状态检测与超时醒目警报
          const hb = leaf.heartbeat;
          if (leaf.state === 'running' || leaf.state === 'claimed') {
            if (hb && hb.source === 'heartbeat-record' && !hb.stale) {
              const age = typeof hb.age_s === 'number' ? Math.round(hb.age_s) : 0;
              const hbBadge = el('span', 'leaf-hb-badge hb-active', `🟢 ${age}s`);
              hbBadge.title = `执行会话心跳活跃（${age}秒前打卡）\n票据：${hb.claim_fingerprint || ''}`;
              leafTop.appendChild(hbBadge);
            } else if (leaf.state === 'running' && hb && hb.stale) {
              const age = typeof hb.age_s === 'number' ? Math.round(hb.age_s) : '>180';
              card.classList.add('hb-warning-yellow');
              const hbBadge = el('span', 'leaf-hb-badge hb-stale', `🟡 超时 ${age}s`);
              hbBadge.title = `会话心跳超时预警：执行者可能已卡住（${age}秒未响应）`;
              leafTop.appendChild(hbBadge);
            } else if (leaf.state === 'running' && (!hb || hb.source === 'none')) {
              card.classList.add('hb-warning-red');
              const hbBadge = el('span', 'leaf-hb-badge hb-missing', `🔴 无心跳`);
              hbBadge.title = `严重异常：当前任务处于执行态但无合法会话心跳！`;
              leafTop.appendChild(hbBadge);
            }
          }

          const description=leaf.blocked_reason?leaf.blocked_reason:leaf.artifact_state==='mismatch'?'回执已保留 · 当前产物与清单不一致':leaf.evidence_error?'记录需核对 · 打开详情查看':leaf.state==='delivered'?(leaf.artifacts?.length||0)+' 项成果 · 可沿成果接手':leaf.state==='awaiting_audit'?'成果已提交 · 等待另一位成员复核':leaf.state==='rejected'?'复核未通过 · 查看意见后再处理':leaf.state==='claimed'?'已接手 · 等待执行心跳':leaf.claimable?'等待接手'+(leaf.recommended_agent?' · 建议 '+memberName(leaf.recommended_agent):''):taskStateLabel(leaf);
          append(card,leafTop,el('p','leaf-description',description));
          const owner=el('span','leaf-owner');owner.dataset.owner=string(leaf.owner||'');owner.title=memberName(leaf.owner);owner.setAttribute('aria-label',memberName(leaf.owner));append(owner,memberLogo(leaf.owner),el('span','leaf-owner-name',memberName(leaf.owner)));const status=el('span','leaf-state',activeWithDrift?`${taskStateLabel(leaf)} · 前序待核对`:taskStateLabel(leaf));if(activeWithDrift)status.title='前序产物状态已变化；当前任务不会被自动中止，下游新任务会暂停。';append(card,append(el('div','leaf-footer'),owner,status),el('span','node-anchor'));
          const actualProgress=leaf.progress_percent??(typeof leaf.progress==='number'?leaf.progress:leaf.progress?.percent)??(leaf.state==='delivered'?100:undefined);
          if(Number.isFinite(actualProgress))status.textContent=Math.max(0,Math.min(100,actualProgress))+'% · '+status.textContent;
          if(Number.isFinite(actualProgress)){const bar=el('div','leaf-progress');bar.setAttribute('role','progressbar');bar.setAttribute('aria-label','总线提供的实际进度');bar.setAttribute('aria-valuenow',String(Math.max(0,Math.min(100,actualProgress))));bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');const filled=el('span');filled.style.width=Math.max(0,Math.min(100,actualProgress))+'%';bar.appendChild(filled);card.appendChild(bar);}
          
          // 注入锚点端口：左入 port-in，右出 port-out
          const portIn = el('span', 'anchor-port port-in');
          portIn.title = '前置依赖输入端口 (接收连线)';
          portIn.dataset.leafId = leaf.id;
          card.appendChild(portIn);

          const portOut = el('span', 'anchor-port port-out');
          portOut.title = '拖拽连线建立前序依赖';
          portOut.dataset.leafId = leaf.id;
          card.appendChild(portOut);

          card.addEventListener('click', () => {
            if (suppressCardClick) return;
            openDetail(leaf);
          });

          // §6.2 双击卡片就地重命名
          card.addEventListener('dblclick', (ev) => {
            if (ev.target.closest('.anchor-port, .leaf-branch-badge, .leaf-priority-badge, button:not(.leaf-card)')) return;
            ev.stopPropagation();
            ev.preventDefault();
            startInPlaceCardRename(card, leaf);
          });

          // 任务卡片 hover 全链路探照高亮
          card.addEventListener('mouseenter',()=>{
            $('forestStage').classList.add('has-hover');
            card.classList.add('node-highlight');
            branchNode.classList.add('node-highlight');
            document.querySelector('.trunk-node')?.classList.add('node-highlight');
            document.querySelectorAll('.forest-lines path').forEach(p=>{
              try{
                const ids=JSON.parse(p.dataset.leafIds||'[]');
                if(ids.includes(leaf.id))p.classList.add('path-highlight');
              }catch{}
            });
          });
          card.addEventListener('mouseleave',()=>{
            $('forestStage').classList.remove('has-hover');
            document.querySelectorAll('.node-highlight').forEach(n=>n.classList.remove('node-highlight'));
            document.querySelectorAll('.path-highlight').forEach(p=>p.classList.remove('path-highlight'));
          });

          // 绑定卡片自由空间拖拽
          bindCardSpatialDragging(card, leaf, curCardX, curCardY, leafW, child.height, curBranchX + branchW, curBranchCenterY);

          // 绑定连线端口拖线
          bindPortWiring(portOut, leaf, leafW, child.height);

          nodes.appendChild(card);

          // 分支 -> 叶子主连线：顺滑三次贝塞尔 S 曲线
          const leafCenterY = curCardY + child.height / 2;
          const span=(cardX-(curBranchX+branchW))*0.5;
          const c1x=curBranchX+branchW+span, c1y=curBranchCenterY;
          const c2x=cardX-span, c2y=leafCenterY;
          const d=(savedPos || spatialPositions[child.parentId]) ? calcHorizontalBezier(curBranchX + branchW, curBranchCenterY, curCardX, leafCenterY) : `M ${curBranchX+branchW} ${curBranchCenterY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${cardX} ${leafCenterY}`;
          const path=treeEdge(d,'leaf',[leaf],String(visual)+(motion?' '+motion:''));
          path.id = 'branch-edge-' + leaf.id;
          tagCollapsibleEdge(path, group.id, child.foldAncestors||[id]);
          path.dataset.branchX = curBranchX + branchW;
          path.dataset.branchY = curBranchCenterY;
          path.dataset.leafId = leaf.id;
          if(motion==='result-arrived')path.style.setProperty('--path-length',path.getTotalLength()+'px');
        }else{
          // nested branch
          const subBranch=child.node;
          const subNode=positioned(el('div','forest-node branch-node nested-branch'),leafX,child.y,branchW,60);
          tagBranchNode(subNode,group.id,subBranch.id,[id],group.root.id,child.descendants.map(leaf=>leaf.id));
          append(subNode,el('span','branch-number','§ '+(subBranch.section_no||'')),el('h3','',shortTitle(subBranch.title||'子分支',13)));
          nodes.appendChild(subNode);

          const subCenterY=child.y+30;
          const span=(leafX-(branchX+branchW))*0.5;
          const c1x=branchX+branchW+span, c1y=cg.centerY;
          const c2x=leafX-span, c2y=subCenterY;
          const d=`M ${branchX+branchW} ${cg.centerY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${leafX} ${subCenterY}`;
          const path=treeEdge(d,'branch',child.descendants,edgeState(child.descendants));
          tagCollapsibleEdge(path, group.id, [id]);
        }
      });
    });

    // 4. 显式前序依赖连线 (Dependencies Wires)
    leaves.forEach(leaf => {
      const deps = Array.isArray(leaf.dependencies) ? leaf.dependencies : [];
      if (!deps.length) return;
      const targetCard = document.getElementById('leaf-node-' + leaf.id);
      if (!targetCard) return;
      deps.forEach(depId => {
        const sourceCard = document.getElementById('leaf-node-' + depId);
        if (!sourceCard) return;
        const srcX = parseFloat(sourceCard.style.left) + leafW;
        const srcY = parseFloat(sourceCard.style.top) + (sourceCard.offsetHeight || 88) / 2;
        const tgtX = parseFloat(targetCard.style.left);
        const tgtY = parseFloat(targetCard.style.top) + (targetCard.offsetHeight || 88) / 2;
        const depD = calcHorizontalBezier(srcX, srcY, tgtX, tgtY);
        const wire = treeEdge(depD, 'dep-wire', [leaf], 'dep-wire');
        wire.classList.add('dep-wire');
        wire.id = `dep-wire-${depId}-${leaf.id}`;
        wire.dataset.depSource = depId;
        wire.dataset.depTarget = leaf.id;
        wire.title = `前序依赖：${depId} ➔ ${leaf.id} (点击可断开)`;
        wire.addEventListener('click', (ev) => {
          ev.stopPropagation();
          showDisconnectPopover(depId, leaf, ev.clientX, ev.clientY);
        });
      });
    });

    if(snapshot.pending_goals.length){
      const pendingX=leafX+leafW+40, pending=positioned(el('button','forest-node pending-goal'),pendingX,Math.max(20,rootCenterY-46),250,92);
      pending.type='button';append(pending,el('span','pending-kicker','新增目标 · 待分解'),el('strong','',shortTitle(snapshot.pending_goals[0].title||snapshot.pending_goals[0].goal,18)),el('span','pending-meta',snapshot.pending_goals.length+' 个正式目标 · 点击查看'));
      pending.addEventListener('click',showPendingGoals);nodes.appendChild(pending);
      line(leafX+leafW,rootCenterY,pendingX,rootCenterY,'claimed',[]);
    }
    syncAllEdgeFlows();applyFocus();transform();
  }
  function leafPriority(leaf) {
    if(!leaf)return '';
    const text=(leaf.goal||'')+' '+(leaf.title||'')+' '+(leaf.body||'');
    const m=text.match(/\b(P[0-3])\b/i)||text.match(/(优先级[：:\s]*)(P[0-3])/i);
    return m?(m[2]||m[1]).toUpperCase():'';
  }
  function matchesQuery(leaf) {
    if(!searchQuery)return true;
    const q=searchQuery.toLowerCase();
    const p=leafPriority(leaf);
    const haystacks=[leaf.section_no,leaf.title,leaf.id,leaf.goal,leaf.task_id,leaf.owner,memberName(leaf.owner),p];
    return haystacks.some(s=>s&&String(s).toLowerCase().includes(q));
  }
  function matches(leaf) {
    const p=leafPriority(leaf);
    const statusMatch=filter==='all'||
      filter==='working'&&working(leaf)||
      filter==='claimed'&&leaf.state==='claimed'||
      filter==='delivered'&&leaf.state==='delivered'||
      filter==='claimable'&&leaf.claimable||
      filter==='blocked'&&(leaf.state==='blocked'||Boolean(leaf.unmet_dependencies&&leaf.unmet_dependencies.length>0))||
      filter==='p0'&&p==='P0'||
      filter==='p1'&&p==='P1'||
      filter==='p2'&&p==='P2'||
      filter==='p3'&&p==='P3';
    return statusMatch&&matchesQuery(leaf);
  }
  function applyFocus() {
    const leaves=snapshot.forest.leaves,byId=new Map(leaves.map(l=>[l.id,l]));
    document.querySelectorAll('.leaf-card').forEach(card=>{const leaf=byId.get(card.dataset.leafId);card.classList.toggle('dimmed',Boolean(focusedMember&&leaf?.owner!==focusedMember));card.classList.toggle('filtered',!leaf||!matches(leaf));card.classList.toggle('selected',leaf?.id===selectedLeaf);card.tabIndex=leaf&&matches(leaf)?0:-1;});
    document.querySelectorAll('.branch-node,.forest-lines path').forEach(node=>{const ids=JSON.parse(node.dataset.leafIds||'[]');const relevant=ids.map(id=>byId.get(id)).filter(Boolean);node.classList.toggle('dimmed',!relevant.some(l=>matches(l)&&(!focusedMember||l.owner===focusedMember)));});
    $('clearFocus').hidden=!focusedMember;$('focusLabel').hidden=!focusedMember;$('focusLabel').textContent=focusedMember?'聚焦 '+memberName(focusedMember):'';
    const count=leaves.filter(l=>matches(l)).length;
    $('footerHint').textContent=filter==='all'&&!searchQuery?'拖拽平移 · 滚轮缩放 · 点击叶子查看交接':count+' 个任务符合筛选 · 其余节点保留结构';
  }
  function viewportKey() {const scope=string(snapshot.project.workspace||snapshot.config.workspace);return scope?'agentbus.viewport:'+encodeURIComponent(scope):'';}
  function saveViewport() {
    const key=viewportKey(),bounds=$('forestViewport')?.getBoundingClientRect();if(!key||!bounds?.width||!bounds?.height)return;
    const viewport={version:1,scale,panX,panY,width:bounds.width,height:bounds.height};
    try{localStorage.setItem(key,JSON.stringify(viewport));}catch{}
    if(bridge()&&online){clearTimeout(viewportSaveTimer);viewportSaveTimer=setTimeout(()=>flushViewport(viewportKey()===key?key:'',viewport),220);}
  }
  async function flushViewport(key=viewportKey(),viewport=null) {
    clearTimeout(viewportSaveTimer);viewportSaveTimer=null;
    if(!key||!bridge()||!online)return false;
    if(!viewport){const bounds=$('forestViewport').getBoundingClientRect();viewport={version:1,scale,panX,panY,width:bounds.width,height:bounds.height};}
    try{const result=await window.pywebview.api.call('save_viewport',{workspace:decodeURIComponent(key.slice('agentbus.viewport:'.length)),viewport});return result?.ok===true;}
    catch{return false;}
  }
  function applyViewport(saved) {
    try{const key=viewportKey(),bounds=$('forestViewport').getBoundingClientRect();if(!key)return false;
      if(!saved||saved.version!==1||![saved.scale,saved.panX,saved.panY,saved.width,saved.height].every(Number.isFinite)||saved.scale<1e-5||saved.scale>8||saved.width<=0||saved.height<=0||Math.abs(saved.panX)>1e7||Math.abs(saved.panY)>1e7)return false;
      fitViewport=false;scale=saved.scale;panX=saved.panX+(bounds.width-saved.width)/2;panY=saved.panY+(bounds.height-saved.height)/2;viewportSize={width:bounds.width,height:bounds.height};transform();saveViewport();return true;
    }catch{return false;}
  }
  function restoreViewport() {
    const key=viewportKey();if(!key)return false;
    try{return applyViewport(JSON.parse(localStorage.getItem(key)||'null'));}catch{return false;}
  }
  async function restoreWorkspaceViewport() {
    const workspace=snapshot.project.workspace||snapshot.config.workspace;
    if(bridge()&&workspace){try{const data=await call('load_viewport',{workspace});if(data?.viewport&&applyViewport(data.viewport))return true;}catch{}}
    return restoreViewport();
  }
  // =========================================================================
  // §6.8 鹰眼全景小地图组件 (CANVAS-MINIMAP)
  // =========================================================================
  const MINIMAP_WIDTH = 200;
  const MINIMAP_HEIGHT = 130;
  let isMinimapDragging = false;

  function getMinimapWorldBounds() {
    let minX = 0, minY = 0, maxX = world.width || 1300, maxY = world.height || 740;
    const allNodes = document.querySelectorAll('.forest-node');
    if (allNodes.length > 0) {
      let fMinX = Infinity, fMinY = Infinity, fMaxX = -Infinity, fMaxY = -Infinity;
      allNodes.forEach(node => {
        const x = parseFloat(node.style.left) || 0;
        const y = parseFloat(node.style.top) || 0;
        const w = Math.max(parseFloat(node.style.width)||0, Number(node.dataset.width)||0, node.offsetWidth||0) || 120;
        const h = Math.max(parseFloat(node.style.height)||0, Number(node.dataset.height)||0, node.offsetHeight||0) || 60;
        if (x < fMinX) fMinX = x;
        if (y < fMinY) fMinY = y;
        if (x + w > fMaxX) fMaxX = x + w;
        if (y + h > fMaxY) fMaxY = y + h;
      });
      if (Number.isFinite(fMinX) && Number.isFinite(fMaxX)) {
        minX = fMinX; minY = fMinY; maxX = fMaxX; maxY = fMaxY;
      }
    }
    const pad = 60;
    minX -= pad;
    minY -= pad;
    maxX += pad;
    maxY += pad;
    return {
      minX,
      minY,
      maxX,
      maxY,
      width: Math.max(200, maxX - minX),
      height: Math.max(130, maxY - minY)
    };
  }

  function updateMinimap() {
    const minimapEl = $('canvasMinimap');
    if (!minimapEl || minimapEl.classList.contains('collapsed')) return;
    const canvas = $('minimapCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.clearRect(0, 0, MINIMAP_WIDTH, MINIMAP_HEIGHT);

    const b = getMinimapWorldBounds();
    const scaleX = MINIMAP_WIDTH / b.width;
    const scaleY = MINIMAP_HEIGHT / b.height;
    const mScale = Math.min(scaleX, scaleY);
    const offsetX = (MINIMAP_WIDTH - b.width * mScale) / 2;
    const offsetY = (MINIMAP_HEIGHT - b.height * mScale) / 2;

    const isDark = document.body.dataset.theme === 'dark';

    // 1. 微型连线简绘
    ctx.strokeStyle = isDark ? 'rgba(255, 255, 255, 0.12)' : 'rgba(0, 0, 0, 0.08)';
    ctx.lineWidth = 1;
    document.querySelectorAll('.forest-lines path').forEach(p => {
      const d = p.getAttribute('d');
      if (!d) return;
      const matches = d.match(/M\s*([\d.-]+)\s+([\d.-]+).*?([\d.-]+)\s+([\d.-]+)$/);
      if (matches) {
        const sx = (parseFloat(matches[1]) - b.minX) * mScale + offsetX;
        const sy = (parseFloat(matches[2]) - b.minY) * mScale + offsetY;
        const ex = (parseFloat(matches[3]) - b.minX) * mScale + offsetX;
        const ey = (parseFloat(matches[4]) - b.minY) * mScale + offsetY;
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(ex, ey);
        ctx.stroke();
      }
    });

    // 2. 微型节点绘制
    document.querySelectorAll('.forest-node').forEach(node => {
      const x = parseFloat(node.style.left) || 0;
      const y = parseFloat(node.style.top) || 0;
      const w = parseFloat(node.style.width) || node.offsetWidth || 120;
      const h = parseFloat(node.style.height) || node.offsetHeight || 60;

      const nx = (x - b.minX) * mScale + offsetX;
      const ny = (y - b.minY) * mScale + offsetY;
      const nw = Math.max(3, w * mScale);
      const nh = Math.max(2.5, h * mScale);

      let color = isDark ? '#64748b' : '#94a3b8';
      if (node.classList.contains('trunk-node')) {
        color = '#3b82f6';
      } else if (node.classList.contains('branch-node')) {
        color = '#8b5cf6';
      } else if (node.classList.contains('leaf-card')) {
        if (node.classList.contains('delivered')) color = '#10b981';
        else if (node.classList.contains('running')) color = '#0284c7';
        else if (node.classList.contains('claimed')) color = '#f59e0b';
        else if (node.classList.contains('blocked') || node.classList.contains('waiting')) color = '#ef4444';
        else color = isDark ? '#475569' : '#cbd5e1';
      } else if (node.classList.contains('sticky-note')) {
        color = '#fde047';
      }

      ctx.fillStyle = color;
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(nx, ny, nw, nh, 1.5);
      } else {
        ctx.rect(nx, ny, nw, nh);
      }
      ctx.fill();
    });

    // 3. 视口高亮指示框更新
    const vp = $('forestViewport');
    const vpBox = $('minimapViewport');
    if (!vp || !vpBox) return;

    const vpRect = vp.getBoundingClientRect();
    const vwLeft = -panX / scale;
    const vwTop = -panY / scale;
    const vwWidth = vpRect.width / scale;
    const vwHeight = vpRect.height / scale;

    const vpX = (vwLeft - b.minX) * mScale + offsetX;
    const vpY = (vwTop - b.minY) * mScale + offsetY;
    const vpW = Math.max(8, vwWidth * mScale);
    const vpH = Math.max(6, vwHeight * mScale);

    vpBox.style.left = vpX + 'px';
    vpBox.style.top = vpY + 'px';
    vpBox.style.width = vpW + 'px';
    vpBox.style.height = vpH + 'px';
  }

  function navigateFromMinimap(e) {
    fitViewport=false;
    const minimapBody = $('minimapBody');
    if (!minimapBody) return;
    const b = getMinimapWorldBounds();
    const bodyRect = minimapBody.getBoundingClientRect();
    const mx = Math.max(0, Math.min(MINIMAP_WIDTH, e.clientX - bodyRect.left));
    const my = Math.max(0, Math.min(MINIMAP_HEIGHT, e.clientY - bodyRect.top));

    const scaleX = MINIMAP_WIDTH / b.width;
    const scaleY = MINIMAP_HEIGHT / b.height;
    const mScale = Math.min(scaleX, scaleY);
    const offsetX = (MINIMAP_WIDTH - b.width * mScale) / 2;
    const offsetY = (MINIMAP_HEIGHT - b.height * mScale) / 2;

    const targetWorldX = (mx - offsetX) / mScale + b.minX;
    const targetWorldY = (my - offsetY) / mScale + b.minY;

    const vpRect = $('forestViewport').getBoundingClientRect();
    panX = vpRect.width / 2 - targetWorldX * scale;
    panY = vpRect.height / 2 - targetWorldY * scale;
    transform();
    saveViewport();
  }

  function initMinimap() {
    const minimapEl = $('canvasMinimap');
    const toggleBtn = $('minimapToggle');
    const minimapBody = $('minimapBody');
    if (!minimapEl || !minimapBody) return;

    try {
      const collapsed = localStorage.getItem('agentbus.minimap-collapsed') === 'true';
      if (collapsed) minimapEl.classList.add('collapsed');
    } catch {}

    toggleBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      const isCollapsed = minimapEl.classList.toggle('collapsed');
      try {
        localStorage.setItem('agentbus.minimap-collapsed', String(isCollapsed));
      } catch {}
      if (!isCollapsed) updateMinimap();
    });

    $('minimapHeader')?.addEventListener('click', (e) => {
      if (e.target === toggleBtn || toggleBtn?.contains(e.target)) return;
      const isCollapsed = minimapEl.classList.toggle('collapsed');
      try {
        localStorage.setItem('agentbus.minimap-collapsed', String(isCollapsed));
      } catch {}
      if (!isCollapsed) updateMinimap();
    });

    minimapBody.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      isMinimapDragging = true;
      navigateFromMinimap(e);
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
      if (!isMinimapDragging) return;
      navigateFromMinimap(e);
    });

    window.addEventListener('mouseup', () => {
      if (isMinimapDragging) {
        isMinimapDragging = false;
      }
    });

    window.addEventListener('resize', () => {
      updateMinimap();
    });
  }

  function transform() {
    $('forestPanLayer').style.left=panX+'px';$('forestPanLayer').style.top=panY+'px';
    $('forestStage').style.zoom=String(scale);
    $('zoomLevel').textContent=Math.round(scale*100)+'%';
    const vp=$('forestViewport');if(vp)vp.style.backgroundPosition=`${panX}px ${panY}px`;
    updateMinimap();
  }
  function zoom(value,atX,atY) {fitViewport=false;const bounds=$('forestViewport').getBoundingClientRect(),x=atX??bounds.width/2,y=atY??bounds.height/2,next=Math.max(1e-5,Math.min(8,value));panX=x-(x-panX)*next/scale;panY=y-(y-panY)*next/scale;scale=next;transform();saveViewport();}
  function fit() {const bounds=$('forestViewport').getBoundingClientRect();if(!bounds.width||!bounds.height)return;fitViewport=true;const scene=getMinimapWorldBounds();viewportSize={width:bounds.width,height:bounds.height};scale=Math.max(1e-5,Math.min(1,Math.max(1,bounds.width-48)/scene.width,Math.max(1,bounds.height-28)/scene.height));panX=(bounds.width-scene.width*scale)/2-scene.minX*scale;panY=(bounds.height-scene.height*scale)/2-scene.minY*scale;transform();saveViewport();}
  function reset() {fitViewport=false;const bounds=$('forestViewport').getBoundingClientRect();viewportSize={width:bounds.width,height:bounds.height};scale=1;panX=30;panY=30;transform();saveViewport();}
  function focusLeaf(leaf) {
    if(!leaf)return false;
    fitViewport=false;
    const layout=forestLayout(),band=layout.bands.find(item=>item.children.some(child=>child.id===leaf.id));
    if(band&&collapsedBranches.has(band.id)){collapsedBranches.delete(band.id);persistCollapsedBranches();renderForest();}
    const card=[...document.querySelectorAll('.leaf-card')].find(item=>item.dataset.leafId===leaf.id);if(!card)return false;
    const bounds=$('forestViewport').getBoundingClientRect(),x=Number(card.dataset.x)||0,y=Number(card.dataset.y)||0,w=Number(card.dataset.width)||320,h=Number(card.dataset.height)||90;
    const layer=$('forestPanLayer');layer.classList.add('focus-motion');panX=bounds.width/2-(x+w/2)*scale;panY=bounds.height/2-(y+h/2)*scale;transform();saveViewport();setTimeout(()=>layer.classList.remove('focus-motion'),420);openDetail(leaf);return true;
  }
  function notifyDependencyReady(previousLeaves,nextLeaves) {
    const current=new Map(nextLeaves.map(leaf=>[leaf.id,leaf]));
    for(const leaf of nextLeaves){const before=previousLeaves.get(leaf.id);if(!before||before.state!=='blocked'||!before.unmet_dependencies?.length||leaf.unmet_dependencies?.length||!leaf.claimable)continue;
      const recovered=(before.unmet_dependencies||[]).map(id=>({old:previousLeaves.get(id),now:current.get(id)})).filter(pair=>pair.now?.state==='delivered'&&pair.now.result_id&&pair.now.dependency_ready===true&&pair.old?.dependency_ready!==true);
      if(!recovered.length)continue;
      const transitionKey=recovered.map(({old,now})=>`${now.id}:${now.result_id}:${old?.state||'unknown'}:${old?.artifact_state||'unknown'}:${(now.artifacts||[]).map(item=>`${item.path||''}:${item.sha256||''}`).sort().join(';')}`).sort().join('|');
      const key='agentbus.ready-notice:'+string(snapshot.project.workspace||snapshot.config.workspace)+':'+leaf.id+':'+transitionKey;
      try{if(localStorage.getItem(key))continue;localStorage.setItem(key,'1');}catch{}
      const producer=recovered[0],message=producer.old?.state==='delivered'?`${numberedTitle(producer.now)} 的产物已通过核对，${numberedTitle(leaf)} 已就绪`:`${numberedTitle(producer.now)} 已交付，${numberedTitle(leaf)} 已就绪`;
      toast(`${message} · 点击查看`,false,()=>focusLeaf(current.get(leaf.id)));break;
    }
  }
  function detailSection(title,text,className='') {const section=el('section','detail-section '+className);append(section,el('h3','',title),el('p','',text||'暂无正式记录。'));return section;}
  function advancedDetails(title,...contents){const details=el('details','advanced-details');append(details,el('summary','',title||'高级信息'),...contents);return details;}
  function toggleDrawerWide() {
    const drawer=$('detailDrawer');
    if(!drawer)return;
    drawer.classList.toggle('wide-mode');
    const isWide=drawer.classList.contains('wide-mode');
    $('drawerWidthToggle')?.setAttribute('aria-pressed',String(isWide));
  }
  function openDetail(leaf) {selectedLeaf=leaf.id;$('detailDrawer').hidden=false;renderDetail(leaf);applyFocus();$('closeDetail').focus();}
  function closeDetail() {const id=selectedLeaf;selectedLeaf='';$('detailDrawer').hidden=true;$('detailDrawer')?.classList.remove('wide-mode');applyFocus();const card=[...document.querySelectorAll('.leaf-card')].find(n=>n.dataset.leafId===id);card?.focus();}
  async function showArtifactDiffDialog(leaf,path) {
    const content=openDialog('产物查看与变更对比 · '+path.split('/').pop(),'文件对比阅读'),version=dialogVersion,loading=el('p','loading-copy','正在读取产物内容与变更对比…');
    content.appendChild(loading);
    try{
      const data=await call('read_artifact_diff',{leaf_id:leaf.id,path});
      if(version!==dialogVersion)return;
      loading.remove();
      if(data.is_image){
        const imgWrap=el('div','dialog-image-preview');
        const img=el('img');img.src=data.image_src;img.alt=path;
        img.style.maxWidth='100%';img.style.maxHeight='500px';img.style.cursor='zoom-in';
        img.addEventListener('click',()=>showImageLightbox(data.image_src));
        append(imgWrap,img,el('p','panel-intro','点击图片开启全屏无遮挡缩放阅读模式'));
        content.appendChild(imgWrap);
      }else{
        const container=el('div','artifact-diff-container');
        const header=el('div','artifact-diff-header');
        append(header,el('span','',`${data.bytes} 字节 · SHA256: ${data.sha256.slice(0,16)}…`),
                      el('span','',data.diff?'已检测到 Git 变更':'无未提交变更 / 当前工作区基线'));
        const body=el('div','artifact-diff-body');
        if(data.diff){
          data.diff.split('\n').forEach(line=>{
            const row=el('span',line.startsWith('+')&&!line.startsWith('+++')?'diff-line-add':line.startsWith('-')&&!line.startsWith('---')?'diff-line-del':line.startsWith('@@')?'diff-line-hunk':'',line+'\n');
            body.appendChild(row);
          });
        }else{
          body.textContent=data.content||'(空文件)';
        }
        append(container,header,body);
        content.appendChild(container);
      }
      const actions=el('div','dialog-action-row');
      const openCode=el('button','button','在 VS Code 中打开'),openCursor=el('button','button','在 Cursor 中打开');
      openCode.type=openCursor.type='button';
      openCode.addEventListener('click',async()=>{try{await call('open_artifact',{leaf_id:leaf.id,path,app:'vscode'});toast('已在 VS Code 中拉起产物。');}catch(e){toast(e.message,true);}});
      openCursor.addEventListener('click',async()=>{try{await call('open_artifact',{leaf_id:leaf.id,path,app:'cursor'});toast('已在 Cursor 中拉起产物。');}catch(e){toast(e.message,true);}});
      append(actions,openCode,openCursor);
      content.appendChild(actions);
    }catch(error){
      if(version===dialogVersion){loading.remove();content.appendChild(el('p','error-note',error.message));}
    }
  }
  async function showAuditDialog(leaf) {
    const content=openDialog('独立审核 · '+numberedTitle(leaf),'审核成果'),version=dialogVersion;
    const loading=el('p','panel-intro','正在读取产物与检查证据…');content.appendChild(loading);
    try{
      const data=await call('audit_context',{leaf_id:leaf.id});
      if(version!==dialogVersion)return;
      loading.remove();
      append(content,el('p','panel-intro',`执行成员：${memberName(data.executor)} · 指定监督员：${memberName(data.auditor)}`),
        el('p','panel-intro','请先让监督员独立核对成果。此窗口代录其结论，系统不会把点击按钮冒称为 AI 已完成审核。'));
      const evidence=data.evidence||{},checks=evidence.checks||[];
      content.appendChild(el('p','',`磁盘产物核验：${evidence.artifact_verification?.verdict==='pass'?'通过':'需检查'}`));
      content.appendChild(el('p','',`文本检查：${(evidence.text_findings||[]).length} 项提示`));
      checks.forEach(check=>content.appendChild(el('p','',`${check.name||'运行检查'}：${check.ok===true?'通过':'未通过'} · 退出码 ${check.exit_code??'未提供'}`)));
      const detail=el('details','advanced-details');append(detail,el('summary','','查看原始成果与检查记录'),el('pre','',data.result_body||''),el('pre','',JSON.stringify(evidence,null,2)));content.appendChild(detail);
      if(!data.can_submit){
        if(data.claimable){const claim=el('button','button primary','以监督员身份认领审核');claim.type='button';claim.disabled=!online;
          claim.addEventListener('click',async()=>{claim.disabled=true;try{const result=await call('claim_audit',{leaf_id:leaf.id,agent:data.auditor});setSnapshot(result);await showAuditDialog(leaf);}catch(error){toast(error.message,true);claim.disabled=!online;}});content.appendChild(claim);
        }else content.appendChild(el('p','import-warning','审核已由其他窗口认领，请在原窗口提交；新窗口不会读取旧票据接管。'));
        return;
      }
      const reasonLabel=el('label','','监督员的复核结论与理由'),reason=el('textarea');reason.maxLength=3800;reasonLabel.appendChild(reason);content.appendChild(reasonLabel);
      const confirmations=[['artifacts_reviewed','已核对磁盘上的实际成果'],['text_scan_reviewed','已检查文本质量与占位问题'],['checks_reviewed','已核对真实运行检查结果'],['operator_confirmed','我已收到指定监督员的独立结论，由我代录']];
      const fields={};confirmations.forEach(([key,label])=>{const row=el('label','external-confirm'),input=el('input');input.type='checkbox';fields[key]=input;append(row,input,el('span','',label));content.appendChild(row);});
      const actions=el('div','dialog-action-row'),approve=el('button','button primary','批准成果'),reject=el('button','button','驳回并记录问题');approve.type=reject.type='button';
      const sync=()=>{const ready=online&&reason.value.trim()&&Object.values(fields).every(input=>input.checked);approve.disabled=!ready||data.can_approve!==true;reject.disabled=!ready;};
      reason.addEventListener('input',sync);Object.values(fields).forEach(input=>input.addEventListener('change',sync));sync();
      if(!data.can_approve)content.appendChild(el('p','import-warning','自动物理检查尚未通过，只能驳回，不能批准。'));
      const submit=async decision=>{approve.disabled=reject.disabled=true;try{const result=await call('submit_audit',{leaf_id:leaf.id,agent:data.auditor,decision,reason:reason.value.trim(),...Object.fromEntries(Object.entries(fields).map(([key,input])=>[key,input.checked]))});setSnapshot(result);closeDialog();toast(decision==='approve'?'独立审核通过，成果已正式交付。':'已记录驳回原因，成果保留待修改。');}catch(error){toast(error.message,true);sync();}};
      approve.addEventListener('click',()=>submit('approve'));reject.addEventListener('click',()=>submit('reject'));append(actions,reject,approve);content.appendChild(actions);
    }catch(error){if(version===dialogVersion){loading.remove();content.appendChild(el('p','error-note',error.message));}}
  }
  function showImageLightbox(imageSrc) {
    const modal=el('div','image-lightbox-modal');
    if(!/^data:image\/(?:png|jpeg|gif|webp|bmp|x-icon);base64,[A-Za-z0-9+/=]+$/.test(imageSrc))return;
    const img=el('img');img.src=imageSrc;img.alt='全屏图片预览';
    modal.appendChild(img);
    modal.addEventListener('click',()=>modal.remove());
    document.body.appendChild(modal);
  }
  function renderDetail(leaf) {
    $('detailTitle').textContent=numberedTitle(leaf);const target=$('detailContent');
    if(target.dataset.leafId){const previous={};target.querySelectorAll('[data-draft-key]').forEach(field=>previous[field.dataset.draftKey]=field.type==='checkbox'?field.checked:field.value);previous.advancedOpen=target.querySelector('.advanced-details')?.open===true;detailDrafts.set(target.dataset.leafId,previous);}
    const draft=detailDrafts.get(leaf.id)||{};target.dataset.leafId=leaf.id;target.replaceChildren();
    const detailStatus=el('div','detail-status');append(detailStatus,el('span','state-pill '+string(leaf.artifact_state==='mismatch'?'artifact-warning':leaf.state==='blocked'&&leaf.unmet_dependencies?.length?'waiting':leaf.state),taskStateLabel(leaf)),el('span','',memberName(leaf.owner)));if(leaf.conversation_id){const session=el('span','member-count','对话 '+leaf.conversation_id.slice(-6));session.title='AgentBus 对话身份：'+leaf.conversation_id;detailStatus.appendChild(session);}target.appendChild(detailStatus);
    if(leaf.blocked_reason){
      const blockBanner=el('div','blocked-diagnosis-banner');
      blockBanner.style.cssText='margin: 10px 0; padding: 10px 14px; border-radius: 6px; background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.25); font-size: 13px; color: #ef4444; line-height: 1.5;';
      blockBanner.appendChild(el('strong','','⚠️ 阻塞诊断（差哪一步）：'));
      blockBanner.appendChild(el('p','',leaf.blocked_reason));
      if(leaf.unmet_dependency_details && leaf.unmet_dependency_details.length){
        const ul=el('ul','');
        ul.style.cssText='margin: 6px 0 0 18px; padding: 0; color: #dc2626;';
        leaf.unmet_dependency_details.forEach(dep=>{
          const li=el('li','',dep.label+'（未交付）');
          ul.appendChild(li);
        });
        blockBanner.appendChild(ul);
      }
      target.appendChild(blockBanner);
    }
    const advanced=advancedDetails('高级 · 原始记录与内容核对');advanced.open=draft.advancedOpen===true;
    if(leaf.evidence_error){
      const mismatch=leaf.artifact_state==='mismatch';
      const evidenceNote=mismatch
        ? '当前文件与交付回执不一致。'+(leaf.state==='admin_passed'&&leaf.dependency_ready?'管理员已允许下游继续，差异仍待核对。':'依赖此产物的任务暂不能继续。')
        : '这项任务的正式记录需要核对。';
      target.appendChild(el('div',mismatch?'artifact-drift-note':'error-note',evidenceNote+'展开高级信息查看原因。'));
      advanced.appendChild(el('p','error-note',leaf.evidence_error));
    }
    target.appendChild(detailSection('目标',leaf.goal));
    const resultText=string(leaf.results),cleanLines=resultText.split('\n').map(s=>s.trim().replace(/^[-*]\s*/,'').replace(/\*\*/g,''));
    const conclusion=cleanLines.find(s=>/^结论\s*[:：]/.test(s));
    const summary=(conclusion&&!/\b[a-f\d]{64}\b|\b[mjr]-[a-f\d-]{14,}/i.test(conclusion)?conclusion:'')||((resultText.length<220&&!/\b[a-f\d]{64}\b|\b[mjr]-[a-f\d-]{14,}/i.test(resultText))?resultText:leaf.state==='delivered'?`正式成果回执已收取。${leaf.artifacts?.length?leaf.artifacts.length+' 项产物路径列在下方。':'该回执未列出产物文件。'}`:taskStateLabel(leaf));
    const results=detailSection('成果',summary.length>220?summary.slice(0,220)+'…':summary);
    if(resultText.length>220||/\b[a-f\d]{64}\b/i.test(resultText))advanced.appendChild(detailSection('完整回执',resultText));target.appendChild(results);target.appendChild(detailSection('接手说明',leaf.handoff,'handoff'));
    const artifacts=el('section','detail-section');artifacts.appendChild(el('h3','','真实产物路径'));
    (leaf.artifacts||[]).forEach(artifact=>{
      const path=string(typeof artifact==='string'?artifact:artifact.path),item=el('div','artifact-item');
      append(item,el('div','artifact-path',path));
      if(typeof artifact!=='string')advanced.appendChild(detailSection(path,`${artifact.bytes??'未提供'} 字节\nSHA256 ${artifact.sha256||'正式回执已验真'}`));
      const row=el('div','artifact-actions-row');
      const copy=el('button','button small','复制路径');copy.type='button';copy.addEventListener('click',()=>copyText(path));row.appendChild(copy);
      if(typeof artifact!=='string'&&leaf.state==='delivered'){
        const vscodeBtn=el('button','button small','VS Code');vscodeBtn.type='button';vscodeBtn.disabled=!online;
        vscodeBtn.title='在 Visual Studio Code 中打开';
        vscodeBtn.addEventListener('click',async()=>{try{await call('open_artifact',{leaf_id:leaf.id,path,app:'vscode'});toast('已在 VS Code 中拉起产物。');}catch(e){toast(e.message,true);}});
        row.appendChild(vscodeBtn);

        const cursorBtn=el('button','button small','Cursor');cursorBtn.type='button';cursorBtn.disabled=!online;
        cursorBtn.title='在 Cursor 中打开';
        cursorBtn.addEventListener('click',async()=>{try{await call('open_artifact',{leaf_id:leaf.id,path,app:'cursor'});toast('已在 Cursor 中拉起产物。');}catch(e){toast(e.message,true);}});
        row.appendChild(cursorBtn);

        const reveal=el('button','button small','资源管理器');reveal.type='button';reveal.disabled=!online;
        reveal.addEventListener('click',async()=>{reveal.disabled=true;try{await call('reveal_artifact',{leaf_id:leaf.id,path});toast('已在资源管理器中定位该产物。');}catch(error){toast(error.message,true);}finally{reveal.disabled=!online;}});
        row.appendChild(reveal);

        const diffBtn=el('button','button small primary','查看变更 Diff');diffBtn.type='button';diffBtn.disabled=!online;
        diffBtn.addEventListener('click',()=>showArtifactDiffDialog(leaf,path));
        row.appendChild(diffBtn);
      }
      item.appendChild(row);
      artifacts.appendChild(item);
    });
    const driftedDependencies=driftedPrerequisites(leaf),activeWithDrift=driftedDependencies.length>0&&['claimed','running'].includes(leaf.state);
    if(driftedDependencies.length){const details=driftedDependencies.map(({dependency,leaf:source,path})=>{const route=[dependency,...path].filter(node=>node&&node.id!==source.id).map(numberedTitle).filter(Boolean).join(' › ');return `${route||'前序任务'} → ${numberedTitle(source)}`;}).join('；');const note=activeWithDrift?`本任务${leaf.state==='running'?'仍在运行':'已认领，实际运行状态未确认'}，但前序产物当前未通过核对：${details}。系统不会自动中止本任务；依赖这些产物的新任务会暂停。请核对输入后再继续。`:`该任务尚未开始，前序产物当前未通过核对：${details}。核对完成前暂不派发，原正式回执保持不变。`;target.appendChild(el('div','artifact-drift-note',note));}
    if(leaf.unmet_dependencies?.length){const {nodesById}=getDriftIndex();const dependencyText=leaf.unmet_dependencies.map(id=>{const dependency=nodesById.get(id);return dependency?numberedTitle(dependency):'前序任务待核对';}).join('\n');target.appendChild(detailSection(driftedDependencies.length?'受影响的前序任务':'先完成这些任务',dependencyText));}
    const actions=el('div','detail-actions'),eligible=Array.isArray(leaf.eligible_agents)?leaf.eligible_agents:[];
    const recommended=leaf.recommended_agent&&snapshot.members.some(member=>member.id===leaf.recommended_agent&&member.enabled)?leaf.recommended_agent:'';
    const suggestion=el('section','recommendation-card');append(suggestion,el('span','recommendation-label','推荐成员'),el('strong','',recommended?memberName(recommended):'尚无匹配成员'),el('p','',leaf.recommendation_reason||'成员能力记录不足，请按任务要求选择。'));
    if(recommended&&!leaf.recommendation_verified)suggestion.appendChild(el('small','','能力待确认 · 建议依据当前登记信息'));target.appendChild(suggestion);
    if(leaf.claimable&&assignmentMode()==='manual'){
      const label=el('label','','指定责任主体'),select=el('select');select.dataset.draftKey='agent';select.setAttribute('aria-label','此任务的责任智能体');eligible.forEach(id=>{const option=el('option','',memberName(id)+(id===recommended?' · 推荐':''));option.value=id;select.appendChild(option);});if(eligible.includes(draft.agent))select.value=draft.agent;else if(recommended&&eligible.includes(recommended))select.value=recommended;label.appendChild(select);append(actions,label);
      if(!leaf.task_id){
        const dispatch=el('button','button primary','派发并生成任务指令');dispatch.disabled=!online||!eligible.length||snapshot.forest.stopped;dispatch.title='把任务交给所选成员，并生成一段接手提示。';dispatch.addEventListener('click',async()=>{const agent=select.value,data=await mutate(dispatch,'dispatch_leaf',{leaf_id:leaf.id,agent});if(!data)return;const current=snapshot.forest.leaves.find(item=>item.id===leaf.id)||leaf;await showTaskInstruction(current,data?.dispatched_to||agent,data?.prompt);});actions.appendChild(dispatch);actions.appendChild(el('p','panel-intro','任务会发到所选成员的项目收件箱；把生成的指令发到该成员的项目对话中，提醒它读取任务并继续工作。'));
        const localAgent=snapshot.config.director||snapshot.project.coordinator||'';
        if(localAgent&&eligible.includes(localAgent)){const claimHere=el('button','button','由 '+memberName(localAgent)+' 在工作台认领');claimHere.disabled=!online||snapshot.forest.stopped;claimHere.title='由你在当前工作台代表协调成员认领；不会验证外部 AI 对话的身份。';claimHere.addEventListener('click',()=>mutate(claimHere,'claim_leaf',{leaf_id:leaf.id,agent:localAgent}));actions.appendChild(claimHere);actions.appendChild(el('p','panel-intro','由你在当前工作台以协调成员身份认领，并保留本会话票据供登记断点。此操作不会验证外部 AI 对话的身份。其他成员请派发任务，由其对话自行认领。'));}
        const preview=el('button','button','预览接手指令');preview.disabled=!online||!eligible.length;preview.addEventListener('click',()=>showTaskInstruction(leaf,select.value));actions.appendChild(preview);
      }else{
        const assigned=leaf.owner||leaf.assigned_agent||leaf.executor||'';
        if(assigned&&eligible.includes(assigned)){select.value=assigned;select.disabled=true;}
        const handoff=el('button','button primary','查看任务指令');handoff.disabled=!eligible.length;handoff.title='查看这项已派发任务的接手提示。';handoff.addEventListener('click',()=>online?showTaskInstruction(leaf,assigned||select.value):copyText(taskHandoffPrompt(leaf,assigned||select.value,snapshot.project.workspace||snapshot.config.workspace),'接手指令已复制，请在成员项目对话中发送。'));actions.appendChild(handoff);actions.appendChild(el('p','panel-intro','任务已发给当前成员。如需更换成员，请先确认原对话已结束，再执行交接，避免两边同时处理同一任务。'));
      }
    }
    else if(leaf.claimable&&assignmentMode()==='automatic'){const note=el('div','assignment-mode-note');const switchManual=el('button','button small','切换为我来选择');switchManual.type='button';switchManual.disabled=!online||assignmentSaving;switchManual.addEventListener('click',async()=>{switchManual.disabled=true;try{await saveAssignmentMode('manual');}catch(error){toast(error.message,true);}finally{switchManual.disabled=!online;}});const switchAutonomous=el('button','button small','切换为自由调度');switchAutonomous.type='button';switchAutonomous.disabled=!online||assignmentSaving;switchAutonomous.addEventListener('click',async()=>{switchAutonomous.disabled=true;try{await saveAssignmentMode('autonomous');}catch(error){toast(error.message,true);}finally{switchAutonomous.disabled=!online;}});append(note,el('p','','当前由组长安排（任务就绪后流转至匹配成员信箱）。也可以切换为我来选择或自由调度。'),switchManual,switchAutonomous);actions.appendChild(note);}
    else if(leaf.claimable&&assignmentMode()==='autonomous'){const note=el('div','assignment-mode-note');const switchAuto=el('button','button small','切换为组长派发');switchAuto.type='button';switchAuto.disabled=!online||assignmentSaving;switchAuto.addEventListener('click',async()=>{switchAuto.disabled=true;try{await saveAssignmentMode('automatic');}catch(error){toast(error.message,true);}finally{switchAuto.disabled=!online;}});const switchManual=el('button','button small','切换为我来选择');switchManual.type='button';switchManual.disabled=!online||assignmentSaving;switchManual.addEventListener('click',async()=>{switchManual.disabled=true;try{await saveAssignmentMode('manual');}catch(error){toast(error.message,true);}finally{switchManual.disabled=!online;}});append(note,el('p','','当前为自由调度模式（各 Agent 可自主通过 claim-next 抢单接活，摆脱组长单点瓶颈）。也可以切换为组长派发或我来选择。'),switchAuto,switchManual);actions.appendChild(note);}
    if(leaf.claimable&&assignmentMode()==='manual'){const note=el('div','assignment-mode-note');const switchAuto=el('button','button small','切换为自动安排');switchAuto.type='button';switchAuto.disabled=!online||assignmentSaving;switchAuto.addEventListener('click',async()=>{switchAuto.disabled=true;try{await saveAssignmentMode('automatic');}catch(error){toast(error.message,true);}finally{switchAuto.disabled=!online;}});const switchAutonomous=el('button','button small','切换为自由调度');switchAutonomous.type='button';switchAutonomous.disabled=!online||assignmentSaving;switchAutonomous.addEventListener('click',async()=>{switchAutonomous.disabled=true;try{await saveAssignmentMode('autonomous');}catch(error){toast(error.message,true);}finally{switchAutonomous.disabled=!online;}});append(note,el('p','','当前由你选择执行成员。也可以切换为自动安排或自由调度让 Agent 自主接活。'),switchAuto,switchAutonomous);actions.appendChild(note);}
    if(!leaf.claimable&&leaf.task_id&&leaf.owner){const original=el('button','button','查看原任务指令');original.type='button';original.disabled=!online;original.addEventListener('click',()=>showTaskInstruction(leaf,leaf.owner));actions.appendChild(original);}
    const ownerEnabled=snapshot.members.some(member=>member.id===leaf.owner&&member.enabled&&member.executor_enabled!==false);
    if(leaf.state==='awaiting_audit'){
      const audit=el('button','button primary','查看并处理独立审核');audit.type='button';audit.disabled=!online;
      audit.addEventListener('click',()=>showAuditDialog(leaf));actions.appendChild(audit);
      actions.appendChild(el('p','panel-intro','成果正在等待指定监督员核对。批准或驳回均会留下正式审核记录。'));
    }
    if(leaf.operator_can_record===true&&leaf.owner&&(leaf.state==='claimed'||leaf.state==='running')){
      const submitBox=el('section','submission-box');
      append(submitBox,el('strong','','成员报告完成后，在这里登记成果'),
        el('p','panel-intro','这会以你的操作人身份写入正式回执。系统会注明“由操作人代录”，不声称已独立验证成员对话。'));
      const summaryLabel=el('label','','成果说明'),summaryField=el('textarea');summaryField.placeholder='简要写下成员报告的完成内容与结论。';summaryField.dataset.draftKey='submittedSummary';summaryField.value=draft.submittedSummary||'';summaryLabel.appendChild(summaryField);
      const pathsLabel=el('label','','产物路径（可选，每行一个相对路径）'),pathsField=el('textarea');pathsField.placeholder='例如：src/module.py';pathsField.dataset.draftKey='submittedArtifacts';pathsField.value=draft.submittedArtifacts||'';pathsLabel.appendChild(pathsField);
      const confirmation=el('label','external-confirm'),checked=el('input');checked.type='checkbox';checked.dataset.draftKey='submitConfirmed';checked.checked=draft.submitConfirmed===true;
      append(confirmation,checked,el('span','','我已从该成员的对话收到完成报告，并确认由我代录这份结果。'));
      const submit=el('button','button primary','提交成果并标记完成');submit.type='button';submit.disabled=!online||!checked.checked;
      checked.addEventListener('change',()=>{submit.disabled=!online||!checked.checked;});
      submit.addEventListener('click',()=>{const summary=summaryField.value.trim();if(!summary)return toast('请填写成果说明。',true);const artifact_paths=pathsField.value.split(/\r?\n/).map(path=>path.trim()).filter(Boolean);mutate(submit,'submit_leaf_result',{leaf_id:leaf.id,agent:leaf.owner,summary,artifact_paths,operator_confirmed:true});});
      append(submitBox,summaryLabel,pathsLabel,confirmation,submit);actions.appendChild(submitBox);
    }
    if((working(leaf)||leaf.state==='claimed'||leaf.state==='running')&&leaf.owner&&leaf.operator_can_record===true){
      const cpBox=el('div','checkpoint-box');
      cpBox.appendChild(el('strong','','阶段断点留痕'));
      cpBox.appendChild(el('p','panel-intro','记录阶段性进展与断点备忘，更新真实进度，不交割任务所有权。'));
      const cpNoteLabel=el('label','','断点进展说明'),cpNote=el('textarea');cpNote.placeholder='记录已完成的阶段性工作、当前卡点或下一步计划...';cpNote.dataset.draftKey='checkpoint';cpNote.value=draft.checkpoint||'';cpNoteLabel.appendChild(cpNote);
      const cpProgLabel=el('label','','阶段进度 (0-100%)'),cpProg=el('input');cpProg.type='number';cpProg.min='0';cpProg.max='100';cpProg.dataset.draftKey='progress';cpProg.value=draft.progress??(Number.isFinite(leaf.progress_percent)?String(leaf.progress_percent):'');cpProgLabel.appendChild(cpProg);
      const cpBtn=el('button','button small','登记断点');cpBtn.disabled=!online;
      cpBtn.addEventListener('click',()=>{
        const summary=cpNote.value.trim();
        if(!summary)return toast('请填写断点进展说明。',true);
        const rawProgress=cpProg.value.trim();
        const prog=rawProgress?Number(rawProgress):undefined;
        if(rawProgress&&(!Number.isInteger(prog)||prog<0||prog>100))return toast('阶段进度需为 0 到 100 的整数。',true);
        const payload={leaf_id:leaf.id,agent:leaf.owner,note:summary};
        if(prog!==undefined)payload.progress=prog;
        mutate(cpBtn,'checkpoint_leaf',payload);
      });
      append(cpBox,cpNoteLabel,cpProgLabel,cpBtn);
      actions.appendChild(cpBox);
    }else if((working(leaf)||leaf.state==='claimed'||leaf.state==='running')&&leaf.owner){
      const checkpointInfo=el('div','checkpoint-readonly-note');
      checkpointInfo.appendChild(el('strong','','当前任务由原认领会话持有'));
      checkpointInfo.appendChild(el('p','panel-intro','当前工作台没有这项任务的有效认领记录，因此不能在这里登记断点。请回到原认领工作台继续，或先通过正式交接让其他成员接手。'));
      actions.appendChild(checkpointInfo);
    }
    const stoppedBinding = Object.freeze({expected_task_id:leaf.task_id, expected_owner:leaf.owner, expected_claim_epoch:leaf.claim_epoch});
    if((working(leaf)||leaf.state==='claimed'||leaf.state==='blocked')&&leaf.owner){actions.appendChild(el('p','panel-intro','此任务由原认领会话持有。确认原会话已停止后，可留下说明交回待接手。'));
      const label=el('label','','接手说明'),note=el('textarea');note.placeholder='写下已有成果、真实断点与下一步。';note.dataset.draftKey='handoff';note.value=draft.handoff||'';label.appendChild(note);
      const confirmation=el('label','external-confirm','我已确认原会话停止'),checked=el('input');checked.type='checkbox';confirmation.prepend(checked);
      const handoff=el('button','button','交回待接手');handoff.disabled=true;checked.addEventListener('change',()=>handoff.disabled=!online||!checked.checked);
      handoff.addEventListener('click',()=>{if(!checked.checked)return;if(!note.value.trim())return toast('请填写接手说明。',true);const operator=snapshot.config.director||snapshot.project.coordinator;if(!operator)return toast('请先选择已启用的组长，由当前成员登记恢复。',true);mutate(handoff,'handoff_leaf',{leaf_id:leaf.id,agent:operator,reason:note.value.trim(),owner_confirmed_inactive:true,...stoppedBinding});});append(actions,label,confirmation,handoff);
    }
    // 智能体安全平滑接管面板
    if(leaf.owner && (working(leaf) || leaf.state==='claimed' || leaf.state==='blocked' || leaf.state==='running')){
      const tkBox = el('div', 'takeover-box');
      tkBox.style.cssText = 'margin-top: 12px; margin-bottom: 12px; padding: 12px; border: 1px solid rgba(14, 165, 233, 0.4); border-radius: 8px; background: rgba(14, 165, 233, 0.05);';
      tkBox.appendChild(el('strong', '', '🤝 平滑转交智能体接手'));
      tkBox.appendChild(el('p', 'panel-intro', '当原智能体超时、额度耗尽、中断或无法继续时，可指派其他智能体安全接手。系统自动释放原锁、继承断点并生成自驱动指令。'));
      
      const agentLabel = el('label', '', '接手智能体');
      const agentSelect = el('select', 'select');
      const candSet = new Set((snapshot.members || []).filter(member => member.enabled===true && member.executor_enabled!==false && !member.observed && member.id!==leaf.owner).map(member => member.id).filter(Boolean));
      candSet.forEach(cand => {
        const opt = el('option', '', memberName(cand) === cand ? cand : `${memberName(cand)} · ${cand}`);
        opt.value = cand;
        agentSelect.appendChild(opt);
      });
      agentLabel.appendChild(agentSelect);
      if(!candSet.size)tkBox.appendChild(el('p','panel-empty','没有其他已启用的执行成员。请先在工作台设置中启用可接手成员。'));
      
      const tkNoteLabel = el('label', '', '接手说明与断点备忘');
      const tkNote = el('textarea');
      tkNote.placeholder = '说明接管原因、当前阶段断点或直接由新 Agent 接手...';
      tkNote.value = leaf.handoff || `由指定智能体接管推进节点 [${leaf.section_no || ''}] ${leaf.title}`;
      tkNoteLabel.appendChild(tkNote);
      
      const tkConfirm = el('label', 'external-confirm', '确认原会话已停止 / 允许安全接管');
      const tkCheck = el('input');
      tkCheck.type = 'checkbox';
      tkCheck.checked = false;
      tkConfirm.prepend(tkCheck);
      
      const tkBtn = el('button', 'button primary small', '🚀 立即接管并复制自驱动指令');
      const updateTakeoverAvailability=()=>{tkBtn.disabled=!online||!candSet.size||tkCheck.checked!==true;};
      updateTakeoverAvailability();tkCheck.addEventListener('change',updateTakeoverAvailability);
      tkBtn.addEventListener('click', async () => {
        const targetAgent = agentSelect.value;
        if(tkCheck.checked!==true||!candSet.has(targetAgent))return;
        const reason = tkNote.value.trim() || `由 ${memberName(targetAgent)} 接管`;
        tkBtn.disabled = true;
        try {
          const res = await call('takeover_leaf', {
            leaf_id: leaf.id,
            agent: targetAgent,
            reason: reason,
            force: tkCheck.checked===true,
            ...stoppedBinding
          });
          if(res && res.capsule && res.capsule.prompt){
            const copied=await copyText(res.capsule.prompt, `已成功接管！已自动复制针对 ${memberName(targetAgent)} 的自驱动执行指令到剪贴板！`);
            if(!copied){
              const content=openDialog('接管完成 · 手动复制指令','任务接手');
              content.appendChild(el('p','panel-intro','任务已接管，但自动复制失败。请选择下方正文后手动复制，再发送给接手成员。'));
              const prompt=el('textarea','workflow-prompt');prompt.readOnly=true;prompt.value=res.capsule.prompt;prompt.rows=14;prompt.setAttribute('aria-label','实际接管指令正文');content.appendChild(prompt);
            }
          } else {
            toast(`已成功接管该任务！已指派给 ${memberName(targetAgent)}`);
          }
          refresh();
        } catch(err) {
          toast(`接管失败: ${err.message}`, true);
          updateTakeoverAvailability();
        }
      });
      
      append(tkBox, agentLabel, tkNoteLabel, tkConfirm, tkBtn);
      actions.appendChild(tkBox);
    }

    const wakeRow = el('div', 'detail-wake-row');
    const wakeAgent = leaf.owner || (leaf.eligible_agents && leaf.eligible_agents[0]) || snapshot.config.director || 'antigravity';
    const copyWakeBtn = el('button', 'button small', '复制专属唤醒指令');
    copyWakeBtn.type = 'button';
    copyWakeBtn.title = '一键复制该任务的专属 Agent 唤醒提示词';
    copyWakeBtn.addEventListener('click', () => {
      const prompt = taskHandoffPrompt(leaf, wakeAgent, snapshot.project.workspace || snapshot.config.workspace);
      copyText(prompt, `已复制针对成员 ${memberName(wakeAgent)} 的专属唤醒提示词！`);
    });
    wakeRow.appendChild(copyWakeBtn);

    const desktopWakeBtn = el('button', 'button small primary', '🔔 桌面原生唤醒');
    desktopWakeBtn.type = 'button';
    desktopWakeBtn.title = '触发 Windows 原生 Toast 通知、提示音，并将自驱动工蜂指令热装载至系统剪贴板';
    desktopWakeBtn.addEventListener('click', async () => {
      try {
        await call('trigger_desktop_wakeup', {
          leaf_id: leaf.id,
          agent: wakeAgent,
          auto_copy: true,
          sound: true
        });
        toast(`🔔 桌面原生唤醒已触发！自驱动工蜂指令已热装载到剪贴板，切换到 ${memberName(wakeAgent)} 窗口按 Ctrl+V 即可！`);
      } catch (err) {
        toast(`桌面唤醒触发失败: ${err.message}`, true);
      }
    });
    wakeRow.appendChild(desktopWakeBtn);

    // 卡死异常任务一键重置打回待办与管理员仲裁放行
    if (leaf.task_id && leaf.owner && ['running', 'claimed', 'blocked'].includes(leaf.state)) {
      const recoverBtn = el('button', 'button small danger', '重置打回待办');
      recoverBtn.type = 'button';
      recoverBtn.disabled = !online;
      recoverBtn.title = '原执行成员卡住或心跳超时无响应时，将该任务从 processing 移除打回待办';
      recoverBtn.addEventListener('click', () => confirmStoppedMember(leaf, async binding => {
        recoverBtn.disabled = true;
        try {
          await call('recover_task', { task_id: binding.task_id, agent: binding.owner, owner_confirmed_inactive: true, expected_claim_epoch: binding.claim_epoch });
          toast(`任务已成功重置打回待办！原认领者 ${memberName(leaf.owner)} 状态已清理`);
          refresh();
        } catch (e) {
          toast(`重置失败: ${e.message}`, true);
          recoverBtn.disabled = !online;
        }
      }));
      wakeRow.appendChild(recoverBtn);
    }
    if (leaf.state !== 'delivered' && (leaf.task_id || ['blocked', 'awaiting_receipt', 'claimed', 'running', 'rejected'].includes(leaf.state))) {
      const resetLeafBtn = el('button', 'button small', '🔄 彻底重置回队列');
      resetLeafBtn.type = 'button';
      resetLeafBtn.disabled = !online;
      resetLeafBtn.title = '重新安排任务；仍受原期限、预算和重试限制';
      resetLeafBtn.addEventListener('click', () => confirmStoppedMember(leaf, binding => mutate(resetLeafBtn, 'reset_leaf', { leaf_id: binding.leaf_id, reason: '工作台详情抽屉执行节点重置回队列', owner_confirmed_inactive: true, expected_task_id: binding.task_id, expected_owner: binding.owner, expected_claim_epoch: binding.claim_epoch })));
      wakeRow.appendChild(resetLeafBtn);
    }
    if (['awaiting_receipt', 'blocked'].includes(leaf.state)) {
      const passLeafBtn = el('button', 'button small primary', '✅ 管理员仲裁放行');
      passLeafBtn.type = 'button';
      passLeafBtn.disabled = !online;
      passLeafBtn.title = '允许下游继续检查其余前序条件，保留本项的成果核验状态';
      passLeafBtn.addEventListener('click', () => mutate(passLeafBtn, 'pass_leaf', { leaf_id: leaf.id, reason: '工作台详情抽屉管理员确认放行' }));
      wakeRow.appendChild(passLeafBtn);
    }
    const discoverBtn = el('button', 'button small', '🌱 衍生新任务');
    discoverBtn.type = 'button';
    discoverBtn.disabled = !online;
    discoverBtn.title = 'Beads 风格动态衍生：在执行本任务期间发现新 Bug 或待办时，在同分支下动态追加带溯源链的新任务';
    discoverBtn.addEventListener('click', () => {
      const content = openDialog('衍生新发现任务 · ' + numberedTitle(leaf), 'BEADS DISCOVER');
      const titleLabel = el('label', 'field', '新发现的问题或待办标题'), titleIn = el('input');
      titleIn.placeholder = '例如：修复某模块边界空指针异常';
      titleLabel.appendChild(titleIn);
      const goalLabel = el('label', 'field', '详细说明与验收条件'), goalIn = el('textarea');
      goalIn.rows = 3;
      goalIn.placeholder = '描述问题上下文与修复要求（系统将自动记录 discovered_from 溯源链）';
      goalLabel.appendChild(goalIn);
      const artLabel = el('label', 'field', '预期产物相对路径（逗号分隔，可选）'), artIn = el('input');
      artIn.placeholder = '例如：src/module.py';
      artLabel.appendChild(artIn);
      const submitBtn = el('button', 'button primary', '🌱 立即衍生并挂入当前分支');
      submitBtn.type = 'button';
      submitBtn.addEventListener('click', async () => {
        if (!titleIn.value.trim()) return toast('请填写衍生任务标题', true);
        submitBtn.disabled = true;
        const res = await mutate(submitBtn, 'discover_leaf', {
          from_task_id: leaf.id,
          agent: wakeAgent,
          title: titleIn.value.trim(),
          goal: goalIn.value.trim(),
          expected_artifacts: artIn.value.trim()
        });
        if (res) {
          closeDialog();
          toast('🌱 已在同分支下动态衍生新任务！');
        }
      });
      append(content, el('p', 'panel-intro', '借鉴 Beads (discovered-from) 模式：边干活边演进任务树，不打断当前任务边界，也不遗漏新发现的技术债。'), titleLabel, goalLabel, artLabel, submitBtn);
    });
    wakeRow.appendChild(discoverBtn);

    const repoMapBtn = el('button', 'button small', '🗺️ RepoMap & 经验');
    repoMapBtn.type = 'button';
    repoMapBtn.disabled = !online;
    repoMapBtn.title = '查看 Aider 风格关联代码 AST 符号地图与团队实战避坑经验 (Lessons Learned)';
    repoMapBtn.addEventListener('click', async () => {
      const content = openDialog('关联代码地图与避坑经验 · ' + numberedTitle(leaf), 'REPOMAP & LESSONS');
      const loading = el('p', 'panel-intro', '正在扫描 AST 符号地图与检索匹配经验...');
      content.appendChild(loading);
      try {
        const targetPaths = (leaf.expected_artifacts || []).map(a => typeof a === 'string' ? a : a.path).filter(Boolean);
        const [rmap, ldata] = await Promise.all([
          call('repomap_slice', { target_paths: targetPaths, keywords: [leaf.title || ''] }),
          call('lessons_list', { target_paths: targetPaths, keywords: [leaf.title || ''], limit: 10 })
        ]);
        loading.remove();
        const mapSec = el('section', 'detail-section');
        mapSec.appendChild(el('h3', '', '🗺️ 关联代码结构地图 (Aider-Style AST RepoMap)'));
        const mapPre = el('pre', 'workflow-prompt', rmap.markdown || '（当前任务未关联或未匹配到已有源码文件符号）');
        mapPre.style.maxHeight = '220px';
        mapPre.style.overflow = 'auto';
        mapSec.appendChild(mapPre);
        content.appendChild(mapSec);

        const lesSec = el('section', 'detail-section');
        lesSec.appendChild(el('h3', '', '📌 团队实战避坑经验 (Lessons Learned)'));
        const ul = el('ul', '');
        (ldata.lessons || []).forEach(it => {
          ul.appendChild(el('li', '', `[${it.agent || 'team'}] ${it.lesson}`));
        });
        if (!(ldata.lessons || []).length) ul.appendChild(el('li', '', '暂无匹配的避坑经验记录。'));
        lesSec.appendChild(ul);
        const newLesIn = el('input');
        newLesIn.placeholder = '一句话记录本任务踩坑/避坑经验（后续同模块任务将自动注入）...';
        const addLesBtn = el('button', 'button small primary', '➕ 沉淀经验');
        addLesBtn.type = 'button';
        addLesBtn.style.marginTop = '8px';
        addLesBtn.addEventListener('click', async () => {
          if (!newLesIn.value.trim()) return toast('请填写经验内容', true);
          addLesBtn.disabled = true;
          try {
            await call('lessons_add', { agent: wakeAgent, lesson: newLesIn.value.trim(), task_id: leaf.id, scope_paths: targetPaths });
            ul.appendChild(el('li', '', `[${wakeAgent}] ${newLesIn.value.trim()}`));
            newLesIn.value = '';
            toast('已沉淀至 .agentbus/lessons.jsonl！');
          } catch (e) { toast(e.message, true); }
          finally { addLesBtn.disabled = false; }
        });
        append(lesSec, newLesIn, addLesBtn);
        content.appendChild(lesSec);
      } catch (err) {
        loading.textContent = '加载失败：' + err.message;
      }
    });
    wakeRow.appendChild(repoMapBtn);
    actions.appendChild(wakeRow);

    if(!online)actions.appendChild(el('p','panel-intro','离线预览只读；交回待接手等状态修改需要桌面桥梁。复制接手指令不会认领或送达任务。'));
    if(actions.childNodes.length)target.appendChild(actions);

    // §6.4 折叠式后台实时控制台镜像与耗时监控
    const consoleBox = el('div', 'console-mirror-box');
    const consoleHeader = el('div', 'console-mirror-header');
    const consoleTitle = el('div', 'console-mirror-title');
    consoleTitle.appendChild(el('span', '', '💻 实时终端日志镜像'));
    const hb = leaf.heartbeat;
    const age = (hb && typeof hb.age_s === 'number') ? Math.round(hb.age_s) : null;
    if (age !== null) {
      const durBadge = el('span', 'console-duration-badge' + (age > 180 ? ' timeout' : ''), `心跳活跃: ${age}s${age > 180 ? ' (超时预警)' : ''}`);
      consoleTitle.appendChild(durBadge);
    }
    const consoleLogs = el('div', 'console-mirror-logs');
    const refreshLogsBtn = el('button', 'button small', '刷新');
    refreshLogsBtn.type = 'button';
    refreshLogsBtn.addEventListener('click', loadConsoleLogs);
    append(consoleHeader, consoleTitle, refreshLogsBtn);
    append(consoleBox, consoleHeader, consoleLogs);
    target.appendChild(consoleBox);

    async function loadConsoleLogs() {
      if (!online) {
        consoleLogs.textContent = '[离线预览] 控制台镜像在桌面桥梁连接后实时输出。';
        return;
      }
      try {
        const res = await call('console_mirror', { limit: 30 });
        consoleLogs.replaceChildren();
        if (res && res.lines && res.lines.length) {
          res.lines.forEach(line => {
            const p = el('div', 'console-log-line ' + (line.level || 'info'), line.text);
            consoleLogs.appendChild(p);
          });
          consoleLogs.scrollTop = consoleLogs.scrollHeight;
        } else {
          consoleLogs.textContent = '[就绪] 暂无近期控制台事件。';
        }
      } catch (err) {
        consoleLogs.textContent = '[日志读取异常] ' + err.message;
      }
    }
    loadConsoleLogs();

    advanced.appendChild(el('div','detail-id',`节点 ${leaf.id}\n任务 ${leaf.task_id||'尚未绑定'}\n正式回执 ${leaf.result_id||'尚无'}\n原始状态 ${leaf.state}\n原始位置 ${leaf.location||'未提供'}`));target.appendChild(advanced);
  }
  async function showTaskInstruction(leaf,agent,providedPrompt) {
    const content=openDialog((taskNumber(leaf)?'任务 '+taskNumber(leaf)+' · ':'')+memberName(agent),'任务指令'),version=dialogVersion;
    append(content,el('h3','template-title',leaf.title),el('p','panel-intro',leaf.task_id?'把这段指令发到所选成员的项目对话。成员仍需核对自己的任务并正式认领。':'这只是预览。请先指派任务，再让成员开工。'));
    const loading=el('p','loading-copy','正在生成当前任务的接手指令…');content.appendChild(loading);
    try{const data=providedPrompt?{prompt:providedPrompt,assigned:true}:await call('task_prompt',{leaf_id:leaf.id,agent});if(version!==dialogVersion)return;loading.remove();if(typeof data.prompt!=='string'||!data.prompt.trim())throw new Error('接口没有返回有效的任务指令。');
      const body=el('textarea','workflow-prompt');body.readOnly=true;body.value=data.prompt;body.setAttribute('aria-label','任务 '+(taskNumber(leaf)||leaf.title)+' 的专属指令');
      const copy=el('button','button primary',data.assigned||leaf.task_id?'复制任务指令':'复制预览 · 尚未指派');copy.type='button';copy.addEventListener('click',()=>copyText(data.prompt,data.assigned||leaf.task_id?'任务指令已复制，请发到 '+memberName(agent)+' 的项目对话。':'预览已复制，任务尚未指派。'));append(content,body,copy);
    }catch(error){if(version===dialogVersion){loading.remove();content.appendChild(el('div','error-note',error.message));}}
  }
  async function copyText(text,message='路径已复制。') {
    try { await navigator.clipboard.writeText(text); toast(message); return true; }
    catch {
      const field=el('textarea'),previousFocus=document.activeElement;let copied=false;
      field.value=text;
      try {document.body.appendChild(field);field.select();copied=document.execCommand('copy')===true;}
      catch {}
      finally {field.remove();previousFocus?.focus?.();}
      toast(copied?message:'无法自动复制，请选择正文后手动复制。',!copied);
      return copied;
    }
  }
  async function mutate(button,action,payload) {
    if(button.disabled)return;button.disabled=true;
    try{
      const data=await call(action,payload);
      if(data?.forest||data?.tree||data?.leaves)setSnapshot(data);else await refresh();
      if(action==='submit_leaf_result'){
        toast(data?.submission_state==='awaiting_audit'?'成果已提交，等待独立审核。':'成果已按“操作人代录”写入正式回执。');
      }else{
        toast(action==='checkpoint_leaf'?'断点已保存。':action==='claim_leaf'?'任务已由 '+memberName(payload.agent)+' 在当前工作台认领。':action==='handoff_leaf'?'任务已交回待接手。':action==='dispatch_leaf'?'任务已投递到 '+memberName(data?.dispatched_to)+' 信箱。':'操作已写入正式总线。');
      }
      if(data?.wake_status==='failed')toast(data.wake_error||'任务已派发，但未能通知成员；请在该成员对话里提醒它查看信箱。',true);
      return data;
    }
    catch(error){toast(error.message,true);}finally{button.disabled=!online;}
  }
  function openDialog(title,eyebrow='WORKSPACE') {lastDialogInvoker=document.activeElement;dialogVersion++;$('dialogTitle').textContent=title;$('dialogEyebrow').textContent=eyebrow;$('dialogContent').replaceChildren();if(!$('workspaceDialog').open)$('workspaceDialog').showModal();return $('dialogContent');}
  function closeDialog() {$('workspaceDialog').close();dialogVersion++;lastDialogInvoker?.focus();}
  function confirmStoppedMember(leaf, onConfirm) {
    const binding = Object.freeze({leaf_id:leaf.id, task_id:leaf.task_id, owner:leaf.owner, claim_epoch:leaf.claim_epoch});
    const content = openDialog('确认原成员已停止', '任务恢复');
    append(content, el('p','panel-intro',`任务 ${leaf.section_no || leaf.title}，成员 ${memberName(binding.owner || '')}。请先确认其对话或进程已经停止。心跳超时不能证明它已经停止。`));
    const label = el('label','external-confirm','我已确认原成员的工作已经停止');
    const check = el('input'); check.type='checkbox'; check.id='confirm-stop-checkbox'; label.prepend(check);
    const confirm = el('button','button primary','确认并继续'); confirm.type='button'; confirm.id='confirm-stop-submit'; confirm.disabled=true;
    check.addEventListener('change',()=>{confirm.disabled=!online||!check.checked;});
    confirm.addEventListener('click',async()=>{if(!check.checked||!online)return;confirm.disabled=true;closeDialog();await onConfirm(binding);});
    const cancel = el('button','button','取消'); cancel.type='button'; cancel.addEventListener('click',closeDialog);
    append(content,label,append(el('div','dialog-action-row'),cancel,confirm));
  }
  function showRoot(root) {const content=openDialog('项目目标','项目主干');append(content,detailSection('目标',root?.goal||snapshot.project.goal),detailSection('项目与任务树',`项目 ${projectName()}\n${snapshot.forest.leaves.length} 项任务；${snapshot.forest.leaves.filter(n=>n.state==='delivered').length} 项已有正式交付回执。`),detailSection('状态说明','交付状态依据有效正式回执；产物核对状态单独显示。项目发布仍需集成验收。认领和实际执行分别显示，便于发现需要接力的任务。'),advancedDetails('高级 · 目标记录',el('pre','',`根节点 ${root?.id||'未提供'}`)));}
  function showPendingGoals(){const content=openDialog('待分解的正式目标','目标队列');content.appendChild(el('p','panel-intro','这些目标已进入正式信箱，等待组长分解为工作项。'));
    snapshot.pending_goals.forEach(goal=>{const card=el('article','api-record');append(card,el('h3','',goal.title||goal.goal),el('p','',goal.goal||''),el('p','',`状态：${goal.status||'待分解'} · 接收成员：${memberName(goal.recipient)}`));content.appendChild(card);});}
  function field(label,value,type='text') {const wrapper=el('label','field',label),input=el('input');input.type=type;input.value=string(value);wrapper.appendChild(input);return {wrapper,input};}
  function showTriggerOnboardingDialog(agentId) {
    if(!online||!bridge()) return toast('离线预览不能配置桌面连接，请在 AgentBus 桌面窗口中操作。', true);
    const content = openDialog(memberName(agentId) + ' · 桌面接入向导 (S0-S6)', 'CONNECTOR ONBOARD');
    const container = el('div', 'trigger-wizard-container');
    container.id = 'triggerWizardContainer';
    content.appendChild(container);
    if (window.TriggerOnboardingWizard) {
      window.wizardInstance = new window.TriggerOnboardingWizard(container);
      window.wizardInstance.init(agentId);
    } else {
      container.innerHTML = '<div class="error-note">向导模块尚未加载，请稍后重试。</div>';
    }
  }
  window.agentbusCall = call;
  window.closeDialog = closeDialog;
  window.showTriggerOnboardingDialog = showTriggerOnboardingDialog;

  async function showMemberPrompt(agent) {
    if(!online||!bridge())return toast('只读预览不能生成实时接入指令，请在 AgentBus 桌面中使用。',true);
    const member=snapshot.members.find(item=>item.id===agent);
    if(!member||member.enabled===false)return toast('请先保存并启用此成员，再生成接入指令。',true);
    const content=openDialog(memberName(agent)+' · 初始接入指令','项目流程 · 04'),version=dialogVersion;
    append(content,el('p','panel-intro','把下面的指令发送到该软件当前项目的新对话。复制不会启动会话，也不代表任务已经执行。'));
    const loading=el('div','loading-copy','正在读取当前项目与成员信箱…');content.appendChild(loading);
    try{const data=await call('onboarding_capsule',{agent});if(version!==dialogVersion)return;loading.remove();
      if(typeof data.prompt!=='string'||!data.prompt.trim())throw new Error('正式接口未返回快速接入指令。');
      const body=el('textarea','workflow-prompt');body.readOnly=true;body.value=data.prompt;body.setAttribute('aria-label','成员接入指令正文');
      const copy=el('button','button primary','复制快速接入指令');copy.type='button';copy.addEventListener('click',()=>copyText(data.prompt,'快速接入指令已复制，请发到该成员当前项目的新对话。'));
      append(content,body,copy);
    }catch(error){if(version===dialogVersion){loading.remove();content.appendChild(el('div','error-note',error.message));}}
  }
  function showOneClickOnboardDialog() {
    if(!online||!bridge())return toast('只读预览不能接入新智能体，请在 AgentBus 桌面中使用。',true);
    const content=openDialog('一键接入新智能体 · 自驱动指令','ONE-CLICK ONBOARD'),version=dialogVersion;
    append(content,el('p','panel-intro','输入新智能体标识并选择宿主环境，系统将全自动完成信箱创建、工作流注册与配置同步，并一键生成自包含、开箱即用的自驱动接入指令。复制后直接粘贴发给新 Agent 即可完成对接！'));
    const form=el('form','settings-form');
    const idField=field('智能体标识','','text');
    idField.input.placeholder='例如：claude-code, cursor, trae, gemini';
    idField.input.required=true;
    const nameField=field('显示名称','','text');
    nameField.input.placeholder='例如：Claude Code 或 Trae 助手';
    const presetWrapper=el('label','field','宿主环境预设');
    const presetSelect=el('select');
    [
      {val:'generic',label:'通用智能体'},
      {val:'claude',label:'Claude Code'},
      {val:'cursor',label:'Cursor IDE'},
      {val:'windsurf',label:'Windsurf'}
    ].forEach(opt=>{const option=el('option','',opt.label);option.value=opt.val;presetSelect.appendChild(option);});
    presetWrapper.appendChild(presetSelect);
    const actionRow=el('div','settings-actions');
    const submitBtn=el('button','button primary','⚡ 一键就绪并复制接入指令');
    submitBtn.type='submit';
    actionRow.appendChild(submitBtn);
    append(form,idField.wrapper,nameField.wrapper,presetWrapper,actionRow);
    content.appendChild(form);
    form.addEventListener('submit',async(e)=>{
      e.preventDefault();
      const memberId=idField.input.value.trim();
      if(!memberId)return toast('请填写智能体标识 ID。',true);
      const displayName=nameField.input.value.trim()||memberId;
      const preset=presetSelect.value;
      submitBtn.disabled=true;
      submitBtn.textContent='正在自动就位并生成指令...';
      try{
        const res=await call('one_click_onboard',{member_id:memberId,display_name:displayName,preset:preset});
        if(version!==dialogVersion)return;
        if(!res||!res.prompt)throw new Error('接口未返回生成的接入指令');
        const copied = await copyText(res.prompt,`智能体 [${displayName}] 接入成功！自驱动指令已自动复制到剪贴板，请直接粘贴发送给新 Agent。`);
        if(version!==dialogVersion)return;
        content.replaceChildren();
        const successEyebrow=el('p','panel-intro',`🎉 智能体 [${displayName}] 已成功就绪！信箱与规范已自动创建。${copied?'接入指令已同步复制至剪贴板，你也可以在下方再次复制：':'自动复制失败，请选择下方指令正文后手动复制，或再次尝试复制：'}`);
        const promptArea=el('textarea','workflow-prompt');
        promptArea.readOnly=true;
        promptArea.rows=14;
        promptArea.value=res.prompt;
        const btnGroup=el('div','settings-actions');
        const reCopyBtn=el('button','button primary','📋 再次复制指令');
        reCopyBtn.type='button';
        reCopyBtn.addEventListener('click',()=>copyText(res.prompt,'接入指令已再次复制到剪贴板！'));
        const closeBtn=el('button','button','完成并关闭');
        closeBtn.type='button';
        closeBtn.addEventListener('click',()=>{closeDialog();refresh();});
        append(btnGroup,reCopyBtn,closeBtn);
        append(content,successEyebrow,promptArea,btnGroup);
        refresh();
      }catch(err){
        toast('接入失败：'+(err.message||err),true);
        submitBtn.disabled=false;
        submitBtn.textContent='⚡ 一键就绪并复制接入指令';
      }
    });
  }
  function showSettings(options={}) {
    const content=openDialog('项目与团队设置','项目流程 · 01'),form=el('form','settings-form'),config=snapshot.config;
    const directories=el('section');append(directories,el('h3','','两处目录，一个项目'),el('p','','控制目录保存任务、信箱和记忆；业务成果可以保存到另一块磁盘。'));
    const workspace=field('控制目录',config.workspace||snapshot.project.workspace),output=field('业务产出目录',config.output_root||snapshot.project.output_root||'.');
    [[workspace,'choose_workspace'],[output,'choose_output']].forEach(([item,action])=>{const row=el('div','path-field'),browse=el('button','button small','选择');browse.type='button';browse.disabled=!online;browse.addEventListener('click',async()=>{browse.disabled=true;try{const data=await call(action,{});if(action==='choose_workspace'){if(data&&!data.cancelled&&data.config){setSnapshot(data);closeDialog();requestAnimationFrame(()=>{fit();});toast(`🎉 已切换至新工作空间：${projectName()}，专属任务树已就绪！开始新任务。`);}return;}const path=typeof data==='string'?data:data?.path||data?.workspace||data?.output_root;if(path)item.input.value=path;}catch(error){toast(error.message,true);}finally{browse.disabled=!online;}});item.wrapper.removeChild(item.input);append(row,item.input,browse);item.wrapper.appendChild(row);directories.appendChild(item.wrapper);});
    const team=el('section');append(team,el('h3','','成员与组长'),el('p','','成员平级协作。组长负责规划、路由与汇总；启用入口不代表执行能力已经验证。'));
    const rows=el('div','settings-members'),rowData=[],director=el('select');director.setAttribute('aria-label','组长');
    const supervision=config.supervision||{strict_supervision:false,designated_auditor:'',checks:[]};
    const auditEnabled=el('input'),auditorSelect=el('select'),checksInput=el('textarea');
    auditEnabled.type='checkbox';auditEnabled.setAttribute('id','supervisionEnabled');auditEnabled.checked=Boolean(supervision.strict_supervision);auditEnabled.disabled=!online;
    auditorSelect.setAttribute('id','supervisionAuditor');auditorSelect.setAttribute('aria-label','独立复核成员');
    checksInput.setAttribute('id','supervisionChecks');checksInput.setAttribute('aria-label','独立复核命令清单');checksInput.rows=7;checksInput.spellcheck=false;
    checksInput.value=JSON.stringify(supervision.checks||[],null,2);checksInput.disabled=!online||!auditEnabled.checked;
    function syncAuditor(){const preferred=auditorSelect.value||string(supervision.designated_auditor);auditorSelect.replaceChildren();const empty=el('option','','请选择复核成员');empty.value='';auditorSelect.appendChild(empty);rowData.filter(row=>row.enabled.checked&&row.id.value.trim()&&row.id.value.trim()!==director.value).forEach(row=>{const identity=row.id.value.trim(),option=el('option','',(row.name.value.trim()||identity)+' ('+identity+')');option.value=identity;auditorSelect.appendChild(option);});if([...auditorSelect.options].some(option=>option.value===preferred))auditorSelect.value=preferred;}
    function syncDirector(){const preferred=director.value||string(config.director);director.replaceChildren();rowData.filter(row=>row.enabled.checked&&row.id.value.trim()).forEach(row=>{const identity=row.id.value.trim(),option=el('option','',(row.name.value.trim()||identity)+' ('+identity+')');option.value=identity;director.appendChild(option);});if([...director.options].some(option=>option.value===preferred))director.value=preferred;syncAuditor();}
    function addRow(member={}) {
      const row=el('div','settings-member simple-member'),id=el('input'),name=el('input'),mode=el('select'),enabled=el('input'),check=el('label','member-checkbox','启用');
      id.value=member.id||'';id.placeholder='例如 design-agent';id.setAttribute('aria-label','成员唯一标识');
      name.value=member.name||member.display_name||'';name.placeholder='成员显示名称';name.setAttribute('aria-label','成员显示名称');id.readOnly=Boolean(member.id);
      ['file','cli','acp','sdk'].forEach(value=>{const option=el('option','',routeName(value));option.value=value;mode.appendChild(option);});
      mode.value=member.communication_mode||member.transport||'file';if(!mode.value)mode.value='file';mode.setAttribute('aria-label','此成员的接入方式');
      enabled.type='checkbox';enabled.checked=member.enabled!==false;enabled.setAttribute('aria-label','启用成员');check.prepend(enabled);
      const routeHelp=el('p','member-route-help'),modeDescriptions={file:'通用方式：在该软件的项目对话发送初始指令，再通过文件交接任务。',cli:'独立命令入口：需要该软件自身的 CLI 已登录并完成真实执行验证。',acp:'多轮协议会话：需要该软件提供可用的 ACP 接口并完成执行验证。',sdk:'专用开发接口：需要已安装、认证并验证对应 SDK。'};
      const updateRouteHelp=()=>{routeHelp.textContent=modeDescriptions[mode.value]||modeDescriptions.file;};mode.addEventListener('change',updateRouteHelp);updateRouteHelp();
      const rowState={original:member,id,name,mode,enabled};
      const avatar=el('span','settings-member-avatar');
      const paintIdentity=()=>{const identity=id.value.trim()||name.value.trim()||'AI';avatar.replaceChildren(memberLogo(identity,{id:identity,name:name.value.trim()||identity,identity_icon_data:member.identity_icon_data}));};
      paintIdentity();
      const identityNote=el('span','member-identity-note','内置离线标识 · 新成员自动使用名称缩写');
      const identityDetails=advancedDetails('高级 · 成员唯一标识',id,el('p','panel-intro','任务与信箱使用此标识；已有成员不能改名换身份。新成员请使用字母、数字和连字符。'));if(!member.id)identityDetails.open=true;
      const entry=el('button','button small member-entry','接入指令');entry.type='button';entry.setAttribute('aria-label',(member.display_name||member.name||member.id||'新成员')+' 接入指令');entry.addEventListener('click',()=>{if(!member.id||id.value.trim()!==member.id||!member.enabled||!enabled.checked)return toast('请先保存并启用此成员，再生成接入指令。',true);if(workspace.input.value.trim()!==config.workspace)return toast('请先保存当前项目目录，再生成接入指令。',true);if(mode.value!==(member.communication_mode||member.transport||'file'))return toast('接入方式已改变，请先保存设置再生成对应的初始指令。',true);showMemberPrompt(member.id);});row.appendChild(entry);
      row.replaceChildren(avatar,name,mode,check,routeHelp,identityNote,identityDetails,entry);
      rows.appendChild(row);rowData.push(rowState);id.addEventListener('input',()=>{paintIdentity();syncDirector();});name.addEventListener('input',()=>{paintIdentity();syncDirector();});enabled.addEventListener('change',syncDirector);syncDirector();
    }
    snapshot.members.forEach(addRow);const add=el('button','button small','+ 添加成员');add.type='button';add.addEventListener('click',()=>addRow());const quickOnboard=el('button','button primary small','⚡ 一键接入新智能体向导');quickOnboard.type='button';quickOnboard.style.marginLeft='8px';quickOnboard.addEventListener('click',()=>{closeDialog();showOneClickOnboardDialog();});const addRowWrap=el('div','member-add-row',add,quickOnboard);append(team,rows,addRowWrap);
    const directorLabel=el('label','field','组长');syncDirector();director.value=string(config.director)||director.value;director.addEventListener('change',syncAuditor);directorLabel.appendChild(director);team.appendChild(directorLabel);
    const allocation=el('section'),allocationLabel=el('label','field','任务分配方式'),allocationSelect=el('select');
    const modeLabels={automatic:'组长自动派发（自适应流水线）',autonomous:'自由调度（Agent自主接活抢单）',manual:'我来选择（人工定向委派）'};
    ['automatic','autonomous','manual'].forEach(value=>{const option=el('option','',modeLabels[value]||value);option.value=value;allocationSelect.appendChild(option);});
    allocationSelect.value=assignmentMode();
    allocationLabel.appendChild(allocationSelect);
    append(allocation,el('h3','','任务怎么分配'),allocationLabel,el('p','','自动安排由组长按依赖与负载分配就绪任务；自由调度允许 Agent 自主抢单认领就绪任务（摆脱组长单点瓶颈）；我来选择由你在任务详情中指定成员。切换方式不会撤销已发出的任务。'));
    const audit=el('section','supervision-settings'),auditToggle=el('label','member-checkbox','提交成果后需要另一位成员复核');auditToggle.prepend(auditEnabled);
    const auditIntro=el('p','panel-intro','开启后，成果先进入待复核；指定成员不能接普通任务，并需核对文件、文字和你预先登记的检查。复核通过才算完成。检查命令由你设置，按原样逐项运行，不经 shell。');
    const auditorLabel=el('label','field','复核成员');auditorLabel.appendChild(auditorSelect);
    const checksLabel=el('label','field','复核命令清单（JSON）');checksLabel.appendChild(checksInput);
    const checksHelp=el('p','panel-intro','填写 JSON 数组；每项使用 name、argv（独立参数数组）和 timeout_seconds。不要在命令参数中放凭据。严格模式至少需要一位与组长不同的已启用复核成员和一项检查。');
    const auditDetails=advancedDetails('独立成果复核',auditToggle,auditIntro,auditorLabel,checksLabel,checksHelp);auditDetails.open=Boolean(supervision.strict_supervision||options.expandSupervision);
    auditEnabled.addEventListener('change',()=>{checksInput.disabled=!online||!auditEnabled.checked;auditorSelect.disabled=!online||!auditEnabled.checked;});auditorSelect.disabled=!online||!auditEnabled.checked;
    const gcSection=el('section','gc-settings');
    append(gcSection,el('h3','','历史封存二进制与备份空间清理'),el('p','','一键扫描并清理项目下冗余的 .deprecated_old_version 历史封存二进制及旧备份（唯一正式出厂程序与被文档引用的文件将自动受保护）。'));
    const gcStatus=el('p','panel-intro','点击下方按钮扫描可释放的磁盘空间。');
    const gcRow=el('div','member-add-row');
    const gcScanBtn=el('button','button small','🔍 扫描可回收空间');gcScanBtn.type='button';gcScanBtn.disabled=!online;
    const gcApplyBtn=el('button','button primary small','🧹 立即清理释放空间');gcApplyBtn.type='button';gcApplyBtn.disabled=true;gcApplyBtn.style.marginLeft='8px';
    gcScanBtn.addEventListener('click',async()=>{
      gcScanBtn.disabled=true;
      try{
        const plan=await call('gc_plan',{keep:1,include_deprecated:true,include_packages:false});
        const mb=((plan.reclaim_bytes||0)/1024/1024).toFixed(1);
        const delCount=(plan.delete||[]).length;
        gcStatus.textContent=delCount?`发现 ${delCount} 个可安全清理的历史封存/备份文件，预计可释放 ${mb} MB 磁盘空间（保留最新 1 份及文档引用项）。`:'当前没有多余的历史封存或旧备份文件需要清理。';
        gcApplyBtn.disabled=!online||!delCount;
      }catch(err){toast(err.message,true);}
      finally{gcScanBtn.disabled=!online;}
    });
    gcApplyBtn.addEventListener('click',async()=>{
      gcApplyBtn.disabled=true;
      try{
        const res=await call('gc_apply',{keep:1,include_deprecated:true,include_packages:false,confirmed:true});
        const mb=((res.reclaimed_bytes||0)/1024/1024).toFixed(1);
        gcStatus.textContent=`✅ 已成功清理 ${res.removed||0} 个历史冗余文件，释放 ${mb} MB 磁盘空间！`;
        toast(`已清理 ${res.removed||0} 个文件，释放 ${mb} MB 空间！`);
      }catch(err){toast(err.message,true);gcApplyBtn.disabled=!online;}
    });
    append(gcRow,gcScanBtn,gcApplyBtn);append(gcSection,gcStatus,gcRow);
    const actions=el('div','settings-actions'),note=el('p','',online?'保存后可为各成员生成一次初始接入指令。':'当前为只读预览，设置不会保存。'),save=el('button','button primary','保存团队与流程');save.type='submit';save.disabled=!online;append(actions,note,save);append(form,directories,team,allocation,auditDetails,gcSection,actions);
    form.addEventListener('submit',async event=>{event.preventDefault();if(rowData.some(row=>row.enabled.checked&&!row.id.value.trim()))return toast('新成员需要填写高级信息中的唯一标识。',true);const members=rowData.map(row=>({id:row.id.value.trim(),display_name:row.name.value.trim()||row.id.value.trim(),enabled:row.enabled.checked,communication_mode:row.mode.value})).filter(m=>m.id);if(!workspace.input.value.trim())return toast('请填写项目控制目录。',true);if(new Set(members.map(m=>m.id)).size!==members.length)return toast('成员唯一标识不能重复。',true);if(!members.some(m=>m.id===director.value.trim()&&m.enabled))return toast('组长需要是一个已启用的成员。',true);let auditChecks;try{auditChecks=JSON.parse(checksInput.value||'[]');if(!Array.isArray(auditChecks))throw new Error();}catch{return toast('复核命令清单需要是合法的 JSON 数组。',true);}const supervisionPayload={strict_supervision:auditEnabled.checked,designated_auditor:auditorSelect.value||'',checks:auditChecks};save.disabled=true;stopAutomaticDispatch();try{const data=await call('configure',{workspace:workspace.input.value.trim(),output_root:output.input.value.trim()||'.',coordinator:director.value.trim(),members,assignment_mode:allocationSelect.value,supervision:supervisionPayload});if(data?.forest){setSnapshot(data);void ensureProjectGuide(data.project);}else await refresh();closeDialog();toast(data?.workflow_ready===false?'项目设置已保存，但接入工作流尚未准备：'+(data.workflow_error||'请检查原文件并重新保存。'):'项目设置已保存。下一步建立任务树，再生成成员初始指令。',data?.workflow_ready===false);}catch(error){toast(error.message,true);save.disabled=!online;}});content.appendChild(form);
    if(options.expandSupervision)requestAnimationFrame(()=>{if(!auditDetails.open)return;auditDetails.scrollIntoView({block:'nearest',behavior:window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches?'auto':'smooth'});auditDetails.querySelector('summary')?.focus({preventScroll:true});});
  }
  function recordsFrom(data,view) {if(Array.isArray(data))return data;if(!data||typeof data!=='object')return[];for(const key of [view,'items','records','events','skills','candidates','tasks','entries','results','actions','issues','profiles','installed'])if(Array.isArray(data[key]))return data[key];return Object.keys(data).length?[data]:[];}
  async function showView(view) {
    const titles={skills:'技能中心',recovery:'接力与恢复',activity:'项目活动'},content=openDialog(titles[view],view.toUpperCase()),version=dialogVersion;
    append(content,el('p','panel-intro',view==='skills'?'管理团队技能生态。支持一键自动扫描项目与宿主环境中的技能清单，实时比对激活状态。':view==='recovery'?'查看正式恢复记录与接力断点，保留原任务所有权与历史。':'查看正式总线产生的项目事件。'));

    let scanStatus=null;
    if(view==='skills'){
      const toolbar=el('div','skills-toolbar');
      const scanBtn=el('button','button primary small','🔍 自动扫描宿主技能');scanBtn.type='button';scanBtn.disabled=!online;
      const reloadBtn=el('button','button small','刷新技能');reloadBtn.type='button';reloadBtn.disabled=!online;
      scanStatus=el('span','skills-scan-status',online?'支持深度检索本地与宿主环境技能注册表':'离线预览只读');
      append(toolbar,scanBtn,reloadBtn,scanStatus);
      content.appendChild(toolbar);

      scanBtn.addEventListener('click',async()=>{
        scanBtn.disabled=true;
        scanStatus.textContent='正在全盘扫描 .skills/ 及宿主技能注册表...';
        try{
          const res=await call('skills',{operation:'scan'});
          if(version!==dialogVersion)return;
          scanStatus.textContent=`扫描完成：发现 ${res.discovered_count||0} 个技能，活跃 ${res.active_count||0} 个`;
          toast(`技能自动扫描完成，共识别 ${res.discovered_count||0} 项技能`);
          renderSkillRecords(res);
        }catch(err){
          scanStatus.textContent='扫描失败：'+err.message;
          toast('扫描失败：'+err.message,true);
        }finally{scanBtn.disabled=!online;}
      });
      reloadBtn.addEventListener('click',()=>showView('skills'));
    }

    const recordsContainer=el('div','records-container');
    content.appendChild(recordsContainer);

    function renderSkillRecords(data){
      recordsContainer.replaceChildren();
      let records=recordsFrom(data,'skills');
      if(data&&typeof data==='object'){
        const discovered = Array.isArray(data.discovered) ? data.discovered.map(d=>({...d, discovered: true, title: d.name, description: d.description + (d.scope==='host'?' · 宿主环境自动扫描发现':' · 本地工程发现')})) : [];
        records=[
          ...discovered,
          ...(Array.isArray(data.entries)?data.entries:records),
          ...(data.project_workflows||[]).map(workflow=>({...workflow,title:memberName(workflow.member)+' · 公共工作流',description:workflow.present?'公共工作流就绪，具备契约接入定义':'公共工作流文件待初始化'}))
        ];
      }
      if(!records.length){recordsContainer.appendChild(el('div','panel-empty','当前没有已激活或发现的技能。点击上方“安全自动扫描”可探测本地宿主环境技能。'));return;}
      records.forEach(record=>{
        const card=el('article','api-record skill-card');
        const header=el('div','skill-card-header');
        const title=el('h3','',record.title||record.name||record.id||'未知技能');
        const badge=el('span','skill-badge '+(record.discovered?'discovered':'active'),record.discovered?'🔍 宿主自动发现':'✓ 项目已激活');
        append(header,title,badge);
        card.appendChild(header);
        const desc=record.description||record.note||'暂无描述';
        card.appendChild(el('p','skill-desc',typeof desc==='object'?JSON.stringify(desc):desc));
        if(record.path)card.appendChild(el('p','artifact-path',record.path));
        const details=advancedDetails('高级 · 技能清单配置',el('pre','',JSON.stringify(record,null,2)));
        card.appendChild(details);recordsContainer.appendChild(card);
      });
    }

    const loading=el('div','loading-copy','正在读取桌面接口…');recordsContainer.appendChild(loading);
    try{
      let data;if(online&&bridge())data=await call(view,{});else{data=snapshot[view]||snapshot.project[view]||snapshot.forest[view];if(data===undefined)throw new Error('离线快照未包含此视图。连接桌面桥梁后可读取实际记录。');}
      if(version!==dialogVersion)return;loading.remove();
      if(view==='skills'){renderSkillRecords(data);return;}
      let renderedHandoffIndex=false;
      if(view==='recovery'&&data&&typeof data==='object'&&data.handoff_index&&Array.isArray(data.handoff_index.entries)){
        const index=data.handoff_index,section=el('section','recovery-handoff-index');
        append(section,el('h3','recovery-index-title','聊天接手索引'),
          el('p','panel-intro','同一成员可以有多个独立聊天。编号只用于 AgentBus 内部区分，不是平台聊天 ID，也不会继承旧聊天的认领权限。'));
        renderedHandoffIndex=true;
        if(!index.entries.length)section.appendChild(el('div','panel-empty','当前没有已认领或已交接的任务会话。新聊天需要重新认领任务。'));
        const stateLabels={queued:'等待认领',claimed:'已认领，执行状态未确认',running:'正在工作',delivered:'已提交成果',awaiting_receipt:'等待正式回执',awaiting_audit:'等待独立审核',blocked:'暂时受阻',stopped:'已暂停',rejected:'需要修改',handed_off:'已交出，旧聊天不能再提交'};
        index.entries.forEach(entry=>{
          const card=el('article','api-record recovery-index-entry'),header=el('div','recovery-index-header');
          const chapter=entry.section_no?'§'+entry.section_no+' · ':'';
          append(header,el('h3','',chapter+(entry.title||'任务')),el('span','skill-badge '+(entry.relation==='current'?'active':'discovered'),entry.relation==='current'?'当前任务':'历史接手'));
          card.appendChild(header);
          card.appendChild(el('p','recovery-index-meta',`${memberName(entry.agent||'')} · ${stateLabels[entry.state]||entry.state||'状态未知'} · 任务 ${entry.task_id||'未知'}`));
          if(entry.conversation_id){const identity=el('p','recovery-conversation-id');append(identity,el('span','','AgentBus 对话编号'),el('code','',entry.conversation_id));card.appendChild(identity);}
          else card.appendChild(el('p','recovery-index-meta','尚未生成对话编号'));
          if(entry.note)card.appendChild(el('p','recovery-index-note',entry.note));
          if(entry.next_action)card.appendChild(el('p','recovery-index-next',entry.next_action));
          const leaf=snapshot.forest.leaves.find(node=>node.id===entry.leaf_id),actions=el('div','recovery-index-actions');
          if(leaf){const open=el('button','button small','查看任务');open.type='button';open.addEventListener('click',()=>focusLeaf(leaf));actions.appendChild(open);}
          card.appendChild(actions);section.appendChild(card);
        });
        recordsContainer.appendChild(section);
      }
      let records=recordsFrom(data,view);
      if(view==='recovery'&&data&&typeof data==='object'&&Array.isArray(data.observations)){records=data.observations.map(observation=>{const leaf=snapshot.forest.leaves.find(n=>n.task_id===observation.task_id);return {...observation,title:leaf?.title||observation.kind||observation.type||'正式恢复观察',description:observation.reason||observation.message||observation.status||observation.state};});if(!records.length&&data.proposals?.length)records=data.proposals.map(proposal=>({...proposal,title:proposal.title||proposal.action||'恢复建议'}));}
      if(!records.length&&!renderedHandoffIndex)recordsContainer.appendChild(el('div','panel-empty','接口返回：当前没有可展示的记录。'));
      const eventLabels={claim:'成员已接手任务',answer:'提交成果与文件',checkpoint:'保存阶段进展',checkpoint_leaf:'保存阶段进展',handoff:'交回待接手',handoff_leaf:'交回待接手',dispatch:'分配任务',dispatch_leaf:'分配具体任务',start_goal:'创建项目新目标',stop:'停止派工',resume:'恢复派工',configure:'更新项目设置',reject:'驳回或拒绝',timeout:'任务超时提醒',lease_acquired:'系统正在保护任务记录',lease_released:'任务记录保护已结束'};
      records.forEach(record=>{if(typeof record!=='object'||record===null){recordsContainer.appendChild(el('div','api-record',record));return;}const card=el('article','api-record'),when=record.timestamp||record.created_at||record.generated_at||record.time;
        if(when)card.appendChild(el('time','',formatDate(when)));
        const rawTitle=record.title||record.name||record.display_name||record.summary||record.event||record.type||record.action||record.kind||record.id||'项目记录';
        const localized=eventLabels[string(rawTitle).trim()]||eventLabels[string(record.event||record.action||record.type).trim()];
        const title=localized||(/^[a-z_]+$/.test(string(rawTitle))?'项目记录':rawTitle);
        card.appendChild(el('h3','',title));
        const description=record.description||record.message||record.reason||record.note||record.handoff||record.state||record.status||record.source||'';if(description)card.appendChild(el('p','',typeof description==='object'?JSON.stringify(description):description));if(record.path)card.appendChild(el('p','artifact-path',record.path));
        const details=advancedDetails('高级 · 正式记录字段',el('pre','',JSON.stringify(record,null,2)));card.appendChild(details);recordsContainer.appendChild(card);
      });
    }catch(error){if(version===dialogVersion){loading.remove();recordsContainer.appendChild(el('div','error-note',error.message));}}
  }
  function showWorkflowGuide() {
    const content=openDialog('AgentBus 6 步多智能体协同规范','协同指引');
    append(content,el('p','panel-intro','遵循本地优先、契约驱动与证据防撞原则，团队 6 步标准化作业流水线如下：'));
    const guideSteps=[
      {no:'01',title:'组织架构与协调者',desc:'定义成员唯一标识、接入方式与组长；双目录架构确保信箱与业务产出物理隔离。',action:showSettings,btn:'前往配置'},
      {no:'02',title:'构建任务拓扑网',desc:'导入组长已确定的项目任务树，或选择可复用模板；按稳定章节号定位和讨论任务。',action:showTreeBuilder,btn:'选择任务树'},
      {no:'03',title:'选择任务分配方式',desc:'由组长自动安排，或由你逐项选择执行成员。已发出的任务不会因切换而改派。',action:showAssignmentChoice,btn:'选择方式'},
      {no:'04',title:'智能体协议接入',desc:'为各成员生成基线工作流引导指令；成员读取各自信箱并使用正规入口完成任务。',action:showInitialization,btn:'生成指令'},
      {no:'05',title:'启动协同流转',desc:'成员有新鲜执行心跳时，其工位出现轻量工作动效；回执到达后任务连线短暂回流。',action:()=>{closeDialog();if(assignmentMode()==='automatic')startAutomaticDispatch();else $('leafSearch').focus();},btn:'启动流转'},
      {no:'06',title:'过程审计与交接',desc:'阶段进展登记断点；交付必须核验原始字节数与 SHA256 签名，未完成任务安全交接。',action:()=>showView('recovery'),btn:'审计记录'}
    ];
    guideSteps.forEach(s=>{
      const item=el('div','workflow-guide-item');
      append(item,el('span','guide-step-no',s.no),append(el('div','guide-step-copy'),el('strong','',s.title),el('p','',s.desc)));
      const b=el('button','button small',s.btn);b.type='button';b.disabled=!online;b.addEventListener('click',s.action);
      item.appendChild(b);content.appendChild(item);
    });
  }
  async function ensureProjectGuide(project=snapshot.project) {
    if(!bridge()||!project?.configured||quickstartInFlight)return;
    const workspace=string(project.workspace||snapshot.config.workspace);if(!workspace)return;
    const fingerprint=JSON.stringify({workspace,output_root:project.output_root||snapshot.config.output_root,
      roots:snapshot.forest.roots.map(n=>[n.id,n.title,n.goal]),
      branches:snapshot.forest.branches.map(n=>[n.id,n.parent_id,n.title]),
      leaves:snapshot.forest.leaves.map(n=>[n.id,n.section_no,n.title,n.state,n.owner])});
    if(fingerprint===quickstartFingerprint)return;
    quickstartInFlight=true;
    try{const result=await call('ensure_quickstart',{});quickstartFingerprint=fingerprint;
      if(result?.conflict)toast('快速上手页已有手动内容，AgentBus 已保留原文件。',true);
    }catch{ /* Self-manifest creation is best effort; the Help action exposes its result. */ }
    finally{quickstartInFlight=false;}
  }
  async function showSystemManifest() {
    const content=openDialog('系统交底与环境自检','帮助与上手'),version=dialogVersion;
    append(content,el('p','panel-intro','这里检查当前项目是否已接好，并生成只含项目公开信息的快速上手页。检查不会读取凭据，也不会调用模型。'));
    const checks=el('div','system-check-list'),status=el('p','system-check-status','正在检查当前项目…');
    const refreshCheck=el('button','button small','重新检查'),quickstart=el('button','button primary','生成项目快速上手页');
    refreshCheck.type=quickstart.type='button';refreshCheck.disabled=quickstart.disabled=!online;
    const runSystemCheck=async()=>{refreshCheck.disabled=true;status.textContent='正在重新检查…';
      try{const data=await call('system_check',{});if(version!==dialogVersion)return;checks.replaceChildren();
        (data.checks||[]).forEach(item=>{const row=el('article','system-check-card '+(item.ok?'ok':'attention'));
          append(row,el('span','system-check-mark',item.ok?'✓':'!'),append(el('div','system-check-copy'),el('strong','',item.title),el('span','',item.detail)));checks.appendChild(row);});
        status.textContent=`检查完成 · ${data.task_nodes||0} 个任务节点 · ${data.ready||0} 项待处理 · 只读检查`;
      }catch(error){if(version===dialogVersion)status.textContent='检查未完成：'+error.message;}
      finally{refreshCheck.disabled=!online;}};
    refreshCheck.addEventListener('click',runSystemCheck);
    quickstart.addEventListener('click',async()=>{quickstart.disabled=true;status.textContent='正在安全生成快速上手页…';
      try{const result=await call('ensure_quickstart',{});if(version!==dialogVersion)return;
        if(result.status==='conflict')status.textContent='已有同名文件含手动内容，已保留原文件。';
        else status.textContent=(result.status==='created'?'已生成':result.status==='updated'?'已更新':result.status==='unchanged'?'内容已是最新':'处理完成')+'：'+(result.path||'项目快速上手页');
        quickstartFingerprint='';
      }catch(error){if(version===dialogVersion)status.textContent='未能生成：'+error.message;}
      finally{quickstart.disabled=!online;}});
    append(content,checks,append(el('div','dialog-action-row'),refreshCheck,quickstart),status);
    if(online){void runSystemCheck();}else status.textContent='离线预览无法检查本机项目；请从 AgentBus 桌面程序打开此页。';
  }
  function showShortcutsGuide() {
    const content=openDialog('桌面工作台快捷键与极客手感手册','快捷键速查');
    append(content,el('p','panel-intro','支持深度桌面原生快捷键与沉浸式白板操作，按 ? 或 F1 随时呼出：'));
    const sections=[
      {
        title:'🎨 白板空间与笔记编排',
        items:[
          {key:'拖拽卡片 / 分支',desc:'自由 2D 拖移摆放，随心调整重要任务关注度先后距离'},
          {key:'Ctrl+Z / Ctrl+Y',desc:'撤销 / 重做：支持卡片与分支移动、便签增删移、智能布局一键回退与恢复'},
          {key:'Alt+A',desc:'一键智能自适应排版整理，清除自定义偏移复位'},
          {key:'Alt+T',desc:'在光标所在位置就地贴上一张随手记便签（多色/折叠/Markdown）'},
          {key:'鼠标滚轮 / 触摸板',desc:'以光标为中心自由平移与无级平滑缩放'},
          {key:'0 / F',desc:'0 键画布复位至 100%；F 键画布自适应居中全景'},
          {key:'点击卡片 ☆ 标记',desc:'循环切换卡片优先级 (☆普通 -> P0极速 -> P1核心 -> P2稳步)'}
        ]
      },
      {
        title:'🔗 任务装配与交互拓扑',
        items:[
          {key:'Alt+N / N / 双击空白',desc:'在光标所在位置就地极速弹出新建任务卡片'},
          {key:'Alt+W',desc:'跨任务依赖虚线显隐开关'},
          {key:'按住卡片右侧端口拖拽',desc:'拉出贝塞尔橡皮筋动态导线，释放至目标卡片建立前序依赖'},
          {key:'点击依赖连线',desc:'呼出微型确认气泡，一键断开前序依赖'},
          {key:'点击卡片分支徽标',desc:'呼出分支菜单，随心将任务重挂载至任意主分支'}
        ]
      },
      {
        title:'⚡ 效率与窗口控制',
        items:[
          {key:'Ctrl+K / Cmd+K',desc:'呼出全局命令面板：模糊检索常用动作、章节号或成员'},
          {key:'Alt+S / Ctrl+B',desc:'展开 / 收起左侧项目成员工位栏'},
          {key:'/',desc:'快速聚焦任务过滤搜索框'},
          {key:'Esc',desc:'退出搜索、关闭详情抽屉或退出对话框'},
          {key:'F5 / Ctrl+R',desc:'重新读取本地总线快照并刷新视图'},
          {key:'? / F1',desc:'随时呼出此快捷键与操作手册'}
        ]
      }
    ];
    const wrapper=el('div','shortcuts-guide-wrapper');
    sections.forEach(sec=>{
      const secEl=el('div','shortcuts-section');
      secEl.appendChild(el('h4','',sec.title));
      const table=el('div','shortcuts-table');
      sec.items.forEach(s=>{
        const row=el('div','shortcut-row');
        append(row,el('kbd','',s.key),el('span','',s.desc));
        table.appendChild(row);
      });
      secEl.appendChild(table);
      wrapper.appendChild(secEl);
    });
    content.appendChild(wrapper);
  }
  const RELEASE_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;
  const releaseStatusMessages = {
    current: '当前没有可用的新版本。',
    unavailable: '暂时无法检查更新，请稍后重试。',
    no_release: '发布仓库暂未提供正式版本。',
    no_asset: '发布仓库的最新版暂未提供 Windows 便携版文件。',
    not_configured: '此构建尚未绑定发布仓库。',
    disabled: '启动时自动检查更新已关闭。',
    deferred: '更新检查已暂缓，请稍后重试。'
  };
  let availableReleaseVersion = '';
  let releaseCheck = null;
  let releasePreferenceSave = null;

  function checkRelease(automatic) {
    if (releaseCheck?.automatic === automatic) return releaseCheck.promise;
    const previous = releaseCheck?.promise;
    const request = {automatic};
    releaseCheck = request;
    request.promise = (async () => {
      // A manual request after a background check must still reach the bridge as manual.
      if (previous) await previous.catch(() => {});
      const result = await call('check_release', {automatic}, {quiet:true});
      const version = string(result?.version).trim();
      if (result?.status === 'update_available' && version) {
        const isNewNotice = availableReleaseVersion !== version;
        availableReleaseVersion = version;
        if (automatic && isNewNotice) {
          toast(`AgentBus 有新版本 ${version} · 查看更新`, false, () => showReleaseDialog());
        }
      } else if (['current', 'no_release', 'no_asset', 'not_configured'].includes(result?.status)) {
        availableReleaseVersion = '';
      }
      const indicator = $('releaseUpdateIndicator');
      indicator.hidden = !availableReleaseVersion;
      indicator.title = availableReleaseVersion ? `AgentBus 有新版本 ${availableReleaseVersion}` : '';
      return result;
    })().finally(() => {
      if (releaseCheck === request) releaseCheck = null;
    });
    return request.promise;
  }

  function startAutomaticReleaseChecks() {
    const run = () => {
      if (bridge() && !releaseCheck) void checkRelease(true).catch(() => {});
    };
    run();
    setInterval(run, RELEASE_CHECK_INTERVAL_MS);
  }

  async function showReleaseDialog(invoker=$('menuTriggerHelp')) {
    const content = openDialog('检查更新', '帮助');
    lastDialogInvoker = invoker;
    const dialog = dialogVersion;
    const isCurrent = () => dialog === dialogVersion && $('workspaceDialog').open;
    const installed = el('p', 'panel-intro', '当前版本 · 正在读取…');
    const automatic = el('input');
    automatic.type = 'checkbox';
    const automaticLabel = el('label', 'member-checkbox', '启动时自动检查更新');
    automaticLabel.prepend(automatic);
    automaticLabel.title = '启动后检查更新，应用保持打开时每天最多自动检查一次';
    const preferenceError = el('p', 'error-note');
    preferenceError.hidden = true;
    preferenceError.setAttribute('role', 'alert');
    const status = el('p', 'panel-intro', '正在读取更新设置…');
    status.setAttribute('role', 'status');
    const check = el('button', 'button primary', '立即检查');
    const releasePage = el('button', 'button', '打开发布页面');
    check.type = releasePage.type = 'button';
    append(content, installed, automaticLabel, preferenceError, status, append(el('div', 'dialog-action-row'), check, releasePage));

    let preferences = null;
    let busy = false;
    function renderControls() {
      const configured = preferences?.configured === true;
      automatic.checked = configured && preferences.automatic === true;
      automatic.disabled = check.disabled = !configured || busy;
      releasePage.hidden = !configured || !availableReleaseVersion;
      releasePage.disabled = busy;
    }
    renderControls();

    automatic.addEventListener('change', async () => {
      const enabled = automatic.checked;
      busy = true;
      automatic.disabled = check.disabled = releasePage.disabled = true;
      preferenceError.hidden = true;
      releasePreferenceSave = call('set_release_preferences', {automatic:enabled}, {quiet:true});
      try {
        preferences = await releasePreferenceSave;
      } catch {
        if (isCurrent()) {
          preferenceError.hidden = false;
          preferenceError.textContent = '暂时无法保存自动检查设置。';
        }
      } finally {
        releasePreferenceSave = null;
        busy = false;
        if (isCurrent()) renderControls();
      }
    });

    check.addEventListener('click', async () => {
      busy = true;
      renderControls();
      status.textContent = '正在检查更新…';
      try {
        const result = await checkRelease(false);
        if (!isCurrent()) return;
        const version = string(result?.current_version).trim();
        if (version) installed.textContent = `当前版本 · ${version}`;
        status.textContent = result?.status === 'update_available' && availableReleaseVersion
          ? `发现新版本 ${availableReleaseVersion}。`
          : (releaseStatusMessages[result?.status] || releaseStatusMessages.unavailable);
      } catch {
        if (isCurrent()) status.textContent = releaseStatusMessages.unavailable;
      } finally {
        busy = false;
        if (isCurrent()) renderControls();
      }
    });

    releasePage.addEventListener('click', async () => {
      busy = true;
      renderControls();
      try {
        const result = await call('open_release_page', {}, {quiet:true});
        if (result?.opened !== true) throw new Error('发布页面未打开');
      } catch {
        if (isCurrent()) status.textContent = '暂时无法打开发布页面，请重新检查更新。';
      } finally {
        busy = false;
        if (isCurrent()) renderControls();
      }
    });

    try {
      if (releasePreferenceSave) await releasePreferenceSave.catch(() => {});
      if (!isCurrent()) return;
      preferences = await call('release_preferences', {}, {quiet:true});
      if (!isCurrent()) return;
      installed.textContent = `当前版本 · ${string(preferences?.current_version).trim() || '暂时无法读取'}`;
      status.textContent = preferences?.configured === true
        ? (availableReleaseVersion ? `发现新版本 ${availableReleaseVersion}。` : '')
        : releaseStatusMessages.not_configured;
      renderControls();
    } catch {
      if (!isCurrent()) return;
      installed.textContent = '当前版本 · 暂时无法读取';
      status.textContent = bridge() ? '暂时无法读取更新设置。' : '请在 AgentBus 桌面工作台中检查更新。';
    }
  }

  async function showAboutDialog() {
    const content = openDialog('关于 AgentBus', '关于');
    const dialog = dialogVersion;
    const details = detailSection('程序版本与运行环境', '架构协调者：Codex & Antigravity\n环境：Windows 桌面桥梁 (pywebview)');
    const version = el('p', '', '当前版本 · 正在读取…');
    details.insertBefore(version, details.querySelector('p'));
    append(content,
      el('h3','about-brand','AgentBus Enterprise Workspace'),
      el('p','panel-intro','本地优先的多智能体文件协作工作台 · 专业桌面原生体系'),
      detailSection('核心原则','• 成员平级：所有智能体遵循公共工作契约，无绝对特权。\n• 文件互通：基于标准 JSON 任务与 Markdown 产物清单协同。\n• 证据防撞：严格核验字节与 SHA256，阶段断点留痕。\n• 视野最大化：顶部菜单栏高低频分离，任务树画布 100% 舒展。'),
      details
    );
    let currentVersion = '';
    try {
      const preferences = await call('release_preferences', {}, {quiet:true});
      currentVersion = string(preferences?.current_version).trim();
    } catch {}
    if (dialog === dialogVersion && $('workspaceDialog').open) {
      version.textContent = `当前版本 · ${currentVersion || '暂时无法读取'}`;
    }
  }
  function showAppendTaskDialog(){
    const content=openDialog('追加新任务节点','任务树');
    content.appendChild(el('p','panel-intro','向当前任务树挂载新的任务叶子。支持自动分配章节号与依赖防冲突。'));
    const form=el('form','settings-form');
    const titleGroup=el('div','form-group');
    titleGroup.appendChild(el('label','','任务标题'));
    const titleInput=el('input','input');
    titleInput.placeholder='例如：任务树分支形态与布局重构';
    titleInput.required=true;
    titleGroup.appendChild(titleInput);
    const goalGroup=el('div','form-group');
    goalGroup.appendChild(el('label','','任务目标与交付范围'));
    const goalArea=el('textarea','textarea');
    goalArea.rows=3;
    goalArea.placeholder='简要描述任务目标、输入输出或验收边界...';
    goalGroup.appendChild(goalArea);
    const branchGroup=el('div','form-group');
    branchGroup.appendChild(el('label','','所属主枝'));
    const branchSelect=el('select','select');
    const branches=snapshot.forest.branches.length?snapshot.forest.branches:[
      {id:'desktop',title:'桌面 UI 看板树'},
      {id:'skills',title:'技能与计划树'},
      {id:'reliability',title:'可靠性与证据树'},
      {id:'members',title:'成员与记忆树'},
      {id:'release',title:'集成打包树'}
    ];
    branches.forEach(b=>{
      const opt=el('option','',(b.section_no?'§'+b.section_no+' ':'')+(b.title||b.id));
      opt.value=b.id;
      branchSelect.appendChild(opt);
    });
    branchGroup.appendChild(branchSelect);
    const depGroup=el('div','form-group');
    depGroup.appendChild(el('label','','前序依赖项（逗号分隔章节号或 ID）'));
    const depInput=el('input','input');
    depInput.placeholder='例如：1.3, 3.2';
    depGroup.appendChild(depInput);
    const actions=el('div','dialog-action-row');
    const cancelBtn=el('button','button','取消');
    cancelBtn.type='button';
    cancelBtn.addEventListener('click',closeDialog);
    const submitBtn=el('button','button primary','挂载至任务树');
    submitBtn.type='submit';
    actions.appendChild(cancelBtn);
    actions.appendChild(submitBtn);
    append(form,titleGroup,goalGroup,branchGroup,depGroup,actions);
    content.appendChild(form);
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      const title=titleInput.value.trim();
      if(!title)return toast('请输入任务标题',true);
      const goal=goalArea.value.trim()||title;
      const branch=branchSelect.value;
      const deps=depInput.value.trim();
      submitBtn.disabled=true;
      try{
        const res=await call('append_leaf',{
          title,
          goal,
          parent_id:branch,
          dependencies:deps
        });
        const generation=mutationGeneration;
        setSnapshot(await call('snapshot',{}),generation);
        closeDialog();
        toast(`任务已挂载至任务树${res?.leaf?.section_no?' (§'+res.leaf.section_no+')':''}！`);
      }catch(err){
        toast(err.message,true);
        submitBtn.disabled=false;
      }
    });
    requestAnimationFrame(()=>titleInput.focus());
  }
  function updateFilterChecks() {
    const map={
      all:$('checkFilterAll'),
      working:$('checkFilterWorking'),
      claimed:$('checkFilterClaimed'),
      delivered:$('checkFilterDelivered'),
      claimable:$('checkFilterClaimable'),
      blocked:$('checkFilterBlocked'),
      p0:$('checkFilterP0'),
      p1:$('checkFilterP1'),
      p2:$('checkFilterP2'),
      p3:$('checkFilterP3')
    };
    Object.entries(map).forEach(([key,node])=>{if(node)node.textContent=filter===key?'✓':'';});
  }
  function setupMenuBar() {
    const menuBar=$('menuBar');if(!menuBar)return;
    const menuItems=menuBar.querySelectorAll('.menu-item');let openMenu=null;
    function closeAllMenus(){menuItems.forEach(item=>{item.classList.remove('active','open');const trigger=item.querySelector('.menu-trigger');if(trigger)trigger.setAttribute('aria-expanded','false');});openMenu=null;}
    menuItems.forEach((item, menuIdx)=>{
      const trigger=item.querySelector('.menu-trigger');if(!trigger)return;
      trigger.addEventListener('click',e=>{
        e.stopPropagation();
        const isOpen=item.classList.contains('active')||item.classList.contains('open');
        closeAllMenus();
        if(!isOpen){
          item.classList.add('active','open');
          trigger.setAttribute('aria-expanded','true');
          openMenu=item;
        }
      });
      trigger.addEventListener('keydown',e=>{
        if(e.key==='ArrowDown'||e.key==='Enter'||e.key===' '){
          e.preventDefault();
          closeAllMenus();
          item.classList.add('active','open');
          trigger.setAttribute('aria-expanded','true');
          openMenu=item;
          const firstAction=item.querySelector('.menu-action:not([disabled])');
          if(firstAction)firstAction.focus();
        }else if(e.key==='ArrowRight'){
          e.preventDefault();
          const nextIdx=(menuIdx+1)%menuItems.length;
          menuItems[nextIdx]?.querySelector('.menu-trigger')?.focus();
        }else if(e.key==='ArrowLeft'){
          e.preventDefault();
          const prevIdx=(menuIdx-1+menuItems.length)%menuItems.length;
          menuItems[prevIdx]?.querySelector('.menu-trigger')?.focus();
        }
      });
      item.addEventListener('mouseenter',()=>{
        if(openMenu&&openMenu!==item){
          closeAllMenus();
          item.classList.add('active','open');
          trigger.setAttribute('aria-expanded','true');
          openMenu=item;
        }
      });
      const actions=Array.from(item.querySelectorAll('.menu-action'));
      actions.forEach((act, actIdx)=>{
        act.setAttribute('tabindex','-1');
        act.addEventListener('keydown',e=>{
          const available=actions.filter(action=>!action.disabled&&!action.hidden&&action.getAttribute('aria-disabled')!=='true');
          const currentIndex=available.indexOf(act);
          if(e.key==='ArrowDown'){
            e.preventDefault();
            available[(currentIndex+1)%available.length]?.focus();
          }else if(e.key==='ArrowUp'){
            e.preventDefault();
            available[(currentIndex-1+available.length)%available.length]?.focus();
          }else if(e.key==='Home'){
            e.preventDefault();
            available[0]?.focus();
          }else if(e.key==='End'){
            e.preventDefault();
            available[available.length-1]?.focus();
          }else if(e.key==='Escape'){
            e.preventDefault();
            closeAllMenus();
            trigger.focus();
          }else if(e.key==='ArrowRight'){
            e.preventDefault();
            closeAllMenus();
            const nextIdx=(menuIdx+1)%menuItems.length;
            const nextMenu=menuItems[nextIdx];
            if(nextMenu){
              nextMenu.classList.add('active','open');
              nextMenu.querySelector('.menu-trigger')?.setAttribute('aria-expanded','true');
              openMenu=nextMenu;
              nextMenu.querySelector('.menu-action:not([disabled])')?.focus();
            }
          }else if(e.key==='ArrowLeft'){
            e.preventDefault();
            closeAllMenus();
            const prevIdx=(menuIdx-1+menuItems.length)%menuItems.length;
            const prevMenu=menuItems[prevIdx];
            if(prevMenu){
              prevMenu.classList.add('active','open');
              prevMenu.querySelector('.menu-trigger')?.setAttribute('aria-expanded','true');
              openMenu=prevMenu;
              prevMenu.querySelector('.menu-action:not([disabled])')?.focus();
            }
          }else if(e.key==='Tab'){
            closeAllMenus();
          }
        });
      });
    });
    document.addEventListener('click',e=>{if(!e.target.closest('.menubar'))closeAllMenus();});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&openMenu){const trigger=openMenu.querySelector('.menu-trigger');closeAllMenus();trigger?.focus();}if(e.ctrlKey&&e.key.toLowerCase()==='b'){e.preventDefault();$('sidebarToggle')?.click();}});
    menuBar.querySelectorAll('.menu-action').forEach(action=>{action.addEventListener('click',()=>closeAllMenus());});
    if($('menuAppendTaskButton'))$('menuAppendTaskButton').addEventListener('click',showAppendTaskDialog);
    if($('menuWorkspaceFolder'))$('menuWorkspaceFolder').addEventListener('click',async()=>{try{const data=await call('choose_workspace',{});if(data&&!data.cancelled&&data.config){setSnapshot(data);closeDialog();requestAnimationFrame(()=>{fit();});toast(`🎉 已切换至新工作空间：${projectName()}，专属任务树已就绪！开始新任务。`);}}catch(err){toast('切换工作空间失败：'+(err.message||err),true);}});
    if($('menuOutputFolder'))$('menuOutputFolder').addEventListener('click',showSettings);
    if($('menuRefreshAction'))$('menuRefreshAction').addEventListener('click',refresh);
    if($('scanProjectTreesButton'))$('scanProjectTreesButton').addEventListener('click',()=>scanProjectTrees());
    if($('menuModeAuto'))$('menuModeAuto').addEventListener('click',async()=>{try{await saveAssignmentMode('automatic');}catch(err){toast(err.message,true);}});
    if($('menuModeAutonomous'))$('menuModeAutonomous').addEventListener('click',async()=>{try{await saveAssignmentMode('autonomous');}catch(err){toast(err.message,true);}});
    if($('menuModeManual'))$('menuModeManual').addEventListener('click',async()=>{try{await saveAssignmentMode('manual');}catch(err){toast(err.message,true);}});
    if($('menuSupervisorSettings'))$('menuSupervisorSettings').addEventListener('click',()=>showSettings({expandSupervision:true}));
    if($('exportCanvasImageButton'))$('exportCanvasImageButton').addEventListener('click',showCanvasExport);
    if($('menuFullscreenAction'))$('menuFullscreenAction').addEventListener('click',toggleFullscreenCanvas);
    if($('menuHelpQuickstart'))$('menuHelpQuickstart').addEventListener('click',showQuickstartTour);
    if($('menuFitView'))$('menuFitView').addEventListener('click',fit);
    if($('menuResetView'))$('menuResetView').addEventListener('click',reset);
    if($('menuTidyAction'))$('menuTidyAction').addEventListener('click',handleTidyUp);
    if($('menuThemeAction'))$('menuThemeAction').addEventListener('click',()=>$('themeButton')?.click());
    if($('menuSidebarToggle'))$('menuSidebarToggle').addEventListener('click',()=>$('sidebarToggle')?.click());
    if($('menuHelpFlow'))$('menuHelpFlow').addEventListener('click',showWorkflowGuide);
    if($('menuHelpKeys'))$('menuHelpKeys').addEventListener('click',showShortcutsGuide);
    if($('menuHelpManifest'))$('menuHelpManifest').addEventListener('click',showSystemManifest);
    $('menuCheckRelease')?.addEventListener('click', () => showReleaseDialog());
    $('releaseUpdateIndicator')?.addEventListener('click', () => {
      closeAllMenus();
      void showReleaseDialog($('releaseUpdateIndicator'));
    });
    if($('menuAbout'))$('menuAbout').addEventListener('click',showAboutDialog);
    if($('quickAppendTaskBtn'))$('quickAppendTaskBtn').addEventListener('click',showAppendTaskDialog);
    if($('quickTidyBtn'))$('quickTidyBtn').addEventListener('click',handleTidyUp);
    if($('quickToggleWireBtn'))$('quickToggleWireBtn').addEventListener('click',toggleDependencyWires);
    if($('quickModeSwitchBtn'))$('quickModeSwitchBtn').addEventListener('click',async()=>{
      const cur=assignmentMode();
      const next=cur==='autonomous'?'automatic':cur==='automatic'?'manual':'autonomous';
      try{await saveAssignmentMode(next);}catch(err){toast(err.message,true);}
    });
    if($('quickDispatchBtn'))$('quickDispatchBtn').addEventListener('click',()=>{
      $('autoDispatchButton')?.click();
      updateFlowControls();
    });
    if($('quickRefreshBtn'))$('quickRefreshBtn').addEventListener('click',refresh);
    if($('quickFitBtn'))$('quickFitBtn').addEventListener('click',fit);
    if($('quickAddTaskBtn'))$('quickAddTaskBtn').addEventListener('click',showAppendTaskDialog);
  }

  function toggleFullscreenCanvas() {
    const shell = $('appShell');
    if (!shell) return;
    const isFull = shell.classList.toggle('fullscreen-mode');
    if (isFull) {
      if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(() => {});
      }
      const hint = el('div', 'fullscreen-exit-hint', '已进入全屏沉浸画布模式 · 按 F11 或 Esc 退出');
      document.body.appendChild(hint);
      setTimeout(() => hint.remove(), 4200);
      toast('已进入全屏沉浸画布模式 (按 F11 退出)');
    } else {
      if (document.fullscreenElement && document.exitFullscreen) {
        document.exitFullscreen().catch(() => {});
      }
      toast('已退出全屏沉浸模式');
    }
    requestAnimationFrame(() => fit());
  }

  function showQuickstartTour() {
    const content = openDialog('新项目快速上手向导', '入门指南');
    append(content, el('p', 'panel-intro', '欢迎来到 AgentBus！AgentBus 采用本地优先、契约驱动的平级多智能体协同流水线。'));

    const stepsContainer = el('div', 'quickstart-tour-steps');
    const tourItems = [
      { num: '1', title: '配置工作空间与双目录隔离', desc: '在【文件 -> 更换控制目录/产出目录】中设定控制与业务产出物理隔离，保障代码资产安全。' },
      { num: '2', title: '导入或追加任务树契约', desc: '通过【文件 -> 导入任务树模板】或按 Alt+N 快速追加节点，构建完整的 DAG 拓扑网。' },
      { num: '3', title: '智能体接入与流水线调度', desc: '在左侧边栏选定成员，点击【生成初始接入指令】，智能体将按信箱队列自主认领推进。' },
      { num: '4', title: '实时监控与 4K 超清长图导出', desc: '随时按 Alt+L 开启血缘探照灯，按 Alt+T 贴上便签，或一键导出 4K 超清拓扑图交付汇报。' }
    ];

    tourItems.forEach(item => {
      const card = el('div', 'quickstart-step-card');
      const numBadge = el('div', 'quickstart-step-num', item.num);
      const textDiv = el('div', 'quickstart-step-content');
      textDiv.appendChild(el('strong', '', item.title));
      textDiv.appendChild(el('p', '', item.desc));
      append(card, numBadge, textDiv);
      stepsContainer.appendChild(card);
    });
    content.appendChild(stepsContainer);

    const actions = el('div', 'dialog-action-row');
    const startBtn = el('button', 'button primary', '开始使用工作台');
    startBtn.type = 'button';
    startBtn.addEventListener('click', () => {
      closeDialog();
      toast('新手向导已完成，尽情开启多智能体高效协同！');
    });
    const fitBtn = el('button', 'button', '全景适屏');
    fitBtn.type = 'button';
    fitBtn.addEventListener('click', () => {
      closeDialog();
      fit();
    });
    append(actions, startBtn, fitBtn);
    content.appendChild(actions);
  }

  async function refresh() {
    if(refreshInFlight)return;
    // §7.27 自动刷新保住正在编辑和拖拽的内容 (REFRESH-PRESERVE-EDIT)
    const isEditingText = ['INPUT','TEXTAREA'].includes(document.activeElement?.tagName) || Boolean(document.activeElement?.isContentEditable);
    const isInteracting = $('workspaceDialog')?.open ||
      document.querySelector('.in-place-create-card') ||
      document.querySelector('.wire-disconnect-popover') ||
      cardDrag?.active ||
      branchDrag?.active ||
      wireDrag?.active ||
      stickyDrag?.active ||
      drag ||
      sidebarDrag?.active ||
      isEditingText;
    if(isInteracting) return;
    refreshInFlight=true;const generation=mutationGeneration;$('refreshButton').classList.add('processing');
    try{
      if(bridge()){
        setConnection(true);
        const data=await call('snapshot',{});
        if(generation===mutationGeneration){
          const accepted=setSnapshot(data, generation);
          if(accepted!==false) void ensureProjectGuide(data.project);
        } else {
          lastRejectedSnapshot = {
            reason: 'stale_generation_inflight',
            requestGen: generation,
            currentGen: mutationGeneration,
            at: new Date().toISOString()
          };
          console.warn(`[SnapshotGuard] Discarding stale snapshot: requested at gen ${generation}, current gen is ${mutationGeneration}`);
        }
      }else{
        setConnection(false);
        const response=await fetch('preview.json',{cache:'no-store'});
        if(!response.ok)throw new Error('未找到实际预览快照。请在桌面工作台连接项目，或提供 preview.json。');
        if(generation===mutationGeneration){
          setSnapshot(await response.json(), generation);
        }
      }
    }
    catch(error){if(!snapshot.forest.leaves.length){$('canvasEmpty').hidden=false;$('canvasEmpty').querySelector('h2').textContent='暂时无法读取项目';$('canvasEmpty').querySelector('p').textContent=error.message;}$('connectionLabel').textContent=bridge()?'桌面已连接 · 读取失败':'离线预览 · 只读';toast(error.message,true);}
    finally{refreshInFlight=false;$('refreshButton').classList.remove('processing');ensureStickyScope();}
  }
  function inspect() {const style=$('appShell').style;return {ready,online,readonly:!online,filter,searchQuery:(typeof searchQuery!=='undefined'?searchQuery:''),focusedMember,selectedLeaf,theme:document.body.dataset.theme||'light',sidebarCollapsed:$('appShell').classList.contains('sidebar-collapsed'),sidebarWidth:style.getPropertyValue?style.getPropertyValue('--sidebar-width'):style['--sidebar-width']||'',minimapVisible:!$('canvasMinimap')?.classList.contains('collapsed'),callTimeoutMs:DEFAULT_CALL_TIMEOUT_MS,lastCallTimeout,refreshPreserved:Boolean(cardDrag?.active||branchDrag?.active||wireDrag?.active||stickyDrag?.active||drag),isInteracting:Boolean(cardDrag?.active||branchDrag?.active||wireDrag?.active||stickyDrag?.active||drag||document.querySelector('.in-place-create-card')),mutationGeneration,lastRejectedSnapshot,zoom:Math.round(scale*100),pan:{x:panX,y:panY},world,roots:snapshot.forest.roots.length,branches:snapshot.forest.branches.length,leaves:snapshot.forest.leaves.length,pendingGoals:snapshot.pending_goals.length,delivered:snapshot.forest.leaves.filter(n=>n.state==='delivered').length,running:snapshot.forest.leaves.filter(n=>n.state==='running').length,claimed:snapshot.forest.leaves.filter(n=>n.state==='claimed').length,blocked:snapshot.forest.leaves.filter(n=>n.state==='blocked').length,visible:snapshot.forest.leaves.filter(matches).length,members:snapshot.members.map(m=>m.id),presence:snapshot.members.map(m=>({id:m.id,...presenceFor(m,snapshot.forest.leaves)})),workbenchMode:workbenchFor(snapshot.forest.leaves).mode,generated_at:snapshot.forest.generated_at,source:snapshot.forest.source};}
  const SIDEBAR_MIN=180,SIDEBAR_MAX=720,SIDEBAR_DEFAULT=280;
  function setSidebarWidth(value,persist=true){
    const width=Math.max(SIDEBAR_MIN,Math.min(SIDEBAR_MAX,Math.round(Number(value)||SIDEBAR_DEFAULT)));
    $('appShell').style.setProperty('--sidebar-width',width+'px');
    sidebarHandle.setAttribute('aria-valuenow',String(width));
    if(persist)try{localStorage.setItem('agentbus-sidebar-width',String(width));}catch{}
    return width;
  }
  const sidebarHandle=$('sidebarResizeHandle');
  try{setSidebarWidth(localStorage.getItem('agentbus-sidebar-width')||SIDEBAR_DEFAULT,false);}catch{setSidebarWidth(SIDEBAR_DEFAULT,false);}
  sidebarHandle.addEventListener('pointerdown',event=>{
    if(event.button!==0)return;
    sidebarDrag={active:true,pointerId:event.pointerId};
    sidebarHandle.setPointerCapture(event.pointerId);
    $('appShell').classList.add('sidebar-resizing');
    sidebarHandle.classList.add('dragging');
    event.preventDefault();
  });
  sidebarHandle.addEventListener('pointermove',event=>{
    if(!sidebarDrag||!sidebarDrag.active)return;
    // 直接以鼠标物理 X 坐标进行绝对映射，杜绝漂移与迟滞，100% 绝对指哪打哪随拖随停！
    setSidebarWidth(event.clientX,false);
  });
  const endSidebarResize=event=>{
    if(!sidebarDrag)return;
    if(sidebarHandle.hasPointerCapture(sidebarDrag.pointerId)){
      sidebarHandle.releasePointerCapture(sidebarDrag.pointerId);
    }
    sidebarDrag=null;
    $('appShell').classList.remove('sidebar-resizing');
    sidebarHandle.classList.remove('dragging');
    const width=parseInt(getComputedStyle($('appShell')).getPropertyValue('--sidebar-width'),10)||SIDEBAR_DEFAULT;
    setSidebarWidth(width,true);
  };
  sidebarHandle.addEventListener('pointerup',endSidebarResize);
  sidebarHandle.addEventListener('pointercancel',endSidebarResize);
  sidebarHandle.addEventListener('lostpointercapture',endSidebarResize);
  sidebarHandle.addEventListener('dblclick',()=>{setSidebarWidth(SIDEBAR_DEFAULT,true);});
  sidebarHandle.addEventListener('keydown',event=>{
    const width=parseInt(sidebarHandle.getAttribute('aria-valuenow'),10)||SIDEBAR_DEFAULT;
    let next=width;
    if(event.key==='ArrowLeft'||event.key==='ArrowDown')next=width-10;
    else if(event.key==='ArrowRight'||event.key==='ArrowUp')next=width+10;
    else if(event.key==='Home')next=SIDEBAR_MIN;
    else if(event.key==='End')next=SIDEBAR_MAX;
    else return;
    event.preventDefault();
    setSidebarWidth(next,true);
  });
  let sidebarMotionTimer=null;
  $('sidebarToggle').addEventListener('click',()=>{
    const shell=$('appShell'),sidebar=$('memberSidebar'),priorWidth=$('forestViewport').getBoundingClientRect().width;
    const isMobile=matchMedia('(max-width:760px)').matches;
    clearTimeout(sidebarMotionTimer);shell.classList.remove('sidebar-closing','sidebar-opening');
    if(!shell.classList.contains('sidebar-collapsed')){
      const width=sidebar.getBoundingClientRect().width;
      shell.style.setProperty('--sidebar-slide-width',(width||SIDEBAR_DEFAULT)+'px');
      shell.style.setProperty('--sidebar-slide-padding',getComputedStyle(sidebar).padding||'30px 20px 16px');
    }
    if(isMobile)shell.classList.toggle('sidebar-mobile-open');
    else shell.classList.toggle('sidebar-collapsed');
    const open=isMobile?shell.classList.contains('sidebar-mobile-open'):!shell.classList.contains('sidebar-collapsed');
    if(!isMobile&&!reducedMotion())shell.classList.add(open?'sidebar-opening':'sidebar-closing');
    $('sidebarToggle').setAttribute('aria-expanded',String(open));
    $('sidebarToggle').setAttribute('aria-label',open?'收起成员栏':'展开成员栏');
    sidebarMotionTimer=setTimeout(()=>{
      shell.classList.remove('sidebar-closing','sidebar-opening');
      const vp=$('forestViewport');
      if(vp){const bounds=vp.getBoundingClientRect();if(bounds.width&&bounds.height){viewportSize={width:bounds.width,height:bounds.height};if(!drag){const layer=$('forestPanLayer');if(!reducedMotion())layer.classList.add('focus-motion');panX+=(bounds.width-priorWidth)/2;transform();saveViewport();setTimeout(()=>layer.classList.remove('focus-motion'),420);}}}
    },reducedMotion()?0:320);
  });
  $('themeButton').addEventListener('click',()=>{document.body.dataset.theme=document.body.dataset.theme==='dark'?'light':'dark';try{localStorage.setItem('agentbus-theme',document.body.dataset.theme);}catch{}$('themeButton').setAttribute('aria-label',document.body.dataset.theme==='dark'?'切换为浅色主题':'切换为深色主题');});
  try{document.body.dataset.theme=localStorage.getItem('agentbus-theme')==='dark'?'dark':'light';}catch{document.body.dataset.theme='light';}
  $('stopButton').addEventListener('click',()=>{stopAutomaticDispatch();mutate($('stopButton'),snapshot.forest.stopped?'resume':'stop',{});});
  $('importTreeButton').addEventListener('click',showTreeImporter);$('exportTreeButton').addEventListener('click',exportTreeTemplate);if($('exportHandoverButton'))$('exportHandoverButton').addEventListener('click',exportHandover);
  if($('exportBackupButton'))$('exportBackupButton').addEventListener('click',exportBackup);
  if($('restoreBackupButton'))$('restoreBackupButton').addEventListener('click',restoreBackup);
  $('assignmentMode').addEventListener('change',async()=>{try{await saveAssignmentMode($('assignmentMode').value);}catch(error){toast(error.message,true);updateFlowControls();}});
  $('autoDispatchButton').addEventListener('click',()=>autoDispatchActive?stopAutomaticDispatch():startAutomaticDispatch());
  $('refreshButton').addEventListener('click',refresh);$('settingsButton').addEventListener('click',showSettings);$('memberSettings').addEventListener('click',showSettings);
  document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
  $('navTree').addEventListener('click',()=>{if($('workspaceDialog').open)closeDialog();closeDetail();});
  $('clearFocus').addEventListener('click',()=>{focusedMember='';renderMembers();applyFocus();});
  document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(item=>{item.classList.toggle('active',item===button);item.setAttribute('aria-pressed',String(item===button));});updateFilterChecks();applyFocus();}));
  setupMenuBar();updateFilterChecks();
  const leafSearchInput=$('leafSearch');
  if(leafSearchInput){leafSearchInput.addEventListener('input',()=>{searchQuery=leafSearchInput.value.trim();applyFocus();});}
  $('closeDetail').addEventListener('click',closeDetail);
  if($('drawerWidthToggle'))$('drawerWidthToggle').addEventListener('click',toggleDrawerWide);
  $('closeDialog').addEventListener('click',closeDialog);$('workspaceDialog').addEventListener('cancel',()=>{dialogVersion++;lastDialogInvoker?.focus();});
  $('zoomIn').addEventListener('click',()=>zoom(scale*1.2));$('zoomOut').addEventListener('click',()=>zoom(scale/1.2));$('resetView').addEventListener('click',reset);$('fitView').addEventListener('click',fit);
  if($('tidyView'))$('tidyView').addEventListener('click',handleTidyUp);
  const viewport=$('forestViewport');
  viewport.addEventListener('dblclick',handleCanvasDblClick);
  viewport.addEventListener('wheel',event=>{if(event.target.closest('.canvas-controls,.preview-notice'))return;event.preventDefault();const bounds=viewport.getBoundingClientRect();zoom(scale*Math.exp(-event.deltaY*.0013),event.clientX-bounds.left,event.clientY-bounds.top);},{passive:false});
  viewport.addEventListener('pointerdown',event=>{
    if(event.button!==0||event.target.closest('button,input,.forest-node,.branch-node,.leaf-card,.canvas-sticky-note,.in-place-create-card,.preview-notice'))return;
    if(event.shiftKey){
      startMarqueeSelection(event);
      return;
    }
    clearBatchSelection();
    fitViewport=false;drag={x:event.clientX,y:event.clientY,panX,panY,pointerId:event.pointerId};
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('dragging');
  });
  viewport.addEventListener('pointermove',event=>{trackCanvasPointer(event);if(!drag)return;panX=drag.panX+event.clientX-drag.x;panY=drag.panY+event.clientY-drag.y;transform();});
  function endDrag(event){if(!drag)return;if(viewport.hasPointerCapture(drag.pointerId))viewport.releasePointerCapture(drag.pointerId);drag=null;viewport.classList.remove('dragging');saveViewport();}
  viewport.addEventListener('pointerup',endDrag);viewport.addEventListener('pointercancel',endDrag);viewport.addEventListener('lostpointercapture',()=>{drag=null;viewport.classList.remove('dragging');});
  viewport.addEventListener('keydown',event=>{if(event.target!==viewport)return;let handled=true;if(event.key.startsWith('Arrow'))fitViewport=false;if(event.key==='ArrowLeft')panX+=40;else if(event.key==='ArrowRight')panX-=40;else if(event.key==='ArrowUp')panY+=40;else if(event.key==='ArrowDown')panY-=40;else if(['+','='].includes(event.key))zoom(scale*1.2);else if(event.key==='-')zoom(scale/1.2);else if(event.key==='0')reset();else if(event.key.toLowerCase()==='f')fit();else handled=false;if(handled){event.preventDefault();transform();saveViewport();}});
  $('toast').addEventListener('click',()=>{const action=toastAction;toastAction=null;$('toast').disabled=true;$('toast').hidden=true;if(action)action();});
  document.addEventListener('keydown',event=>{
    const isEditingText = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (commandPaletteOpen) {
        closeCommandPalette();
      } else {
        openCommandPalette();
      }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'z') {
      if (!isEditingText) {
        event.preventDefault();
        if (event.shiftKey) {
          performRedo();
        } else {
          performUndo();
        }
        return;
      }
    }
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'y') {
      if (!isEditingText) {
        event.preventDefault();
        performRedo();
        return;
      }
    }
    // §6.2 Ctrl+C / Ctrl+V 卡片克隆
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'c') {
      if (!isEditingText) {
        const target = selectedLeaf ? snapshot?.forest?.leaves?.find(l => l.id === selectedLeaf) :
                       (batchSelectedCardIds.size ? snapshot?.forest?.leaves?.find(l => batchSelectedCardIds.has(l.id)) : null);
        if (target) {
          event.preventDefault();
          clipboardCard = { ...target };
          toast(`已复制任务卡: ${clipboardCard.title || clipboardCard.id} (按 Ctrl+V 粘贴克隆)`);
          return;
        }
      }
    }
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'v') {
      if (!isEditingText && clipboardCard) {
        event.preventDefault();
        executeCloneCard(clipboardCard);
        return;
      }
    }
    if(event.altKey&&event.key.toLowerCase()==='t'){
      event.preventDefault();
      createStickyNote();
      return;
    }
    if(event.altKey&&event.key.toLowerCase()==='a'){
      event.preventDefault();
      handleTidyUp();
      return;
    }
    if(event.altKey&&event.key.toLowerCase()==='s'){
      event.preventDefault();
      $('sidebarToggle')?.click();
      return;
    }
    if(event.key==='F11'){
      event.preventDefault();
      toggleFullscreenCanvas();
      return;
    }
    if((event.key==='?'||event.key==='F1')&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)&&!$('workspaceDialog').open){
      event.preventDefault();
      showShortcutsGuide();
      return;
    }
    if((event.ctrlKey||event.metaKey||event.altKey)&&event.key.toLowerCase()==='n'){
      event.preventDefault();
      showAppendTaskDialog();
      return;
    }
    if(event.altKey&&event.key.toLowerCase()==='w'){
      event.preventDefault();
      toggleDependencyWires();
      return;
    }
    if(event.key.toLowerCase()==='n'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)&&!$('workspaceDialog').open&&!document.querySelector('.in-place-create-card')){
      event.preventDefault();
      showAppendTaskDialog();
      return;
    }
    // §6.1 Ctrl+F 即时搜索呼出
    if (((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'f') ||
        (event.key==='/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName) && !$('workspaceDialog').open)) {
      const searchBox = $('leafSearch');
      if (searchBox) {
        event.preventDefault();
        searchBox.focus();
        searchBox.select();
      }
      return;
    }
    // §6.3 Alt+F 切换抽屉宽屏居中阅读模式
    if (event.altKey && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      if (!$('detailDrawer').hidden) {
        toggleDrawerWide();
      }
      return;
    }
    // §6.1 Alt+L 切换选中任务节点的上下游血缘探照灯模式
    if (event.altKey && event.key.toLowerCase() === 'l') {
      event.preventDefault();
      if (selectedLeaf) {
        toggleSpotlight(selectedLeaf);
      } else {
        toast('请先点击选中一个任务卡片，再开启血缘探照灯。');
      }
      return;
    }
    if(event.key==='Escape'){
      if(spotlightTargetLeafId){
        clearSpotlight();
        return;
      }
      const searchBox=$('leafSearch');
      if($('workspaceDialog').open)return;
      if(!$('detailDrawer').hidden){if(searchBox?.value){searchBox.value='';searchQuery='';applyFocus();}closeDetail();return;}
      if(document.activeElement===searchBox||(searchBox&&searchBox.value)){if(searchBox)searchBox.value='';searchQuery='';applyFocus();if(document.activeElement===searchBox)searchBox.blur();}
      return;
    }
    if(event.key==='Enter'&&(document.activeElement===$('leafSearch')||event.target===$('leafSearch'))){
      const matchedLeaves=snapshot.forest.leaves.filter(leaf=>matches(leaf));
      if(matchedLeaves.length===1){
        event.preventDefault();
        locateCard(matchedLeaves[0].id);
        openDetail(matchedLeaves[0]);
        $('leafSearch').blur();
      }
      else if(matchedLeaves.length>1){event.preventDefault();toast(`找到多个匹配任务（${matchedLeaves.length} 项），请输入更精确的序号或名称。`,true);}
      else if(searchQuery){event.preventDefault();toast('没有找到匹配的任务，请检查序号或名称。',true);}
    }
  });
  document.addEventListener('visibilitychange',()=>{clearTimeout(autoDispatchTimer);autoDispatchTimer=null;if(!document.hidden)scheduleAutomaticDispatch();updateFlowControls();});
  window.addEventListener('beforeunload',()=>{stopAutomaticDispatch();flushViewport();});window.addEventListener('pagehide',()=>{stopAutomaticDispatch();return flushViewport();});
  window.addEventListener('pywebviewready',refresh);window.addEventListener('resize',()=>{const bounds=$('forestViewport').getBoundingClientRect();if(initializedView&&fitViewport&&!drag){fit();return;}if(!viewportSize.width||!viewportSize.height){viewportSize={width:bounds.width,height:bounds.height};return;}if(initializedView&&!drag){panX+=(bounds.width-viewportSize.width)/2;panY+=(bounds.height-viewportSize.height)/2;viewportSize={width:bounds.width,height:bounds.height};transform();saveViewport();}});
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{const bounds=$('forestViewport').getBoundingClientRect();if(fitViewport&&initializedView&&!drag&&(Math.abs(bounds.width-viewportSize.width)>.5||Math.abs(bounds.height-viewportSize.height)>.5))requestAnimationFrame(()=>{if(fitViewport&&!drag)fit();});}).observe($('forestViewport'));
  document.addEventListener('visibilitychange',()=>document.body.classList.toggle('motion-paused',document.hidden));

  // --- §6.1 上下游血缘探照灯模式 (Lineage Spotlight) ---
  let spotlightTargetLeafId = null;

  function findLineage(leafId) {
    if (!leafId || !snapshot?.forest?.leaves) return { ancestors: new Set(), descendants: new Set() };
    const leavesMap = new Map(snapshot.forest.leaves.map(l => [l.id, l]));
    const ancestors = new Set();
    const queueUp = [leafId];
    while (queueUp.length > 0) {
      const currId = queueUp.shift();
      const currLeaf = leavesMap.get(currId);
      if (!currLeaf) continue;
      const deps = [...(currLeaf.dependencies || []), ...(currLeaf.effective_dependencies || [])];
      for (const depId of deps) {
        if (!ancestors.has(depId) && depId !== leafId) {
          ancestors.add(depId);
          queueUp.push(depId);
        }
      }
    }
    const descendants = new Set();
    const queueDown = [leafId];
    while (queueDown.length > 0) {
      const currId = queueDown.shift();
      for (const [otherId, otherLeaf] of leavesMap.entries()) {
        const deps = [...(otherLeaf.dependencies || []), ...(otherLeaf.effective_dependencies || [])];
        if (deps.includes(currId) && !descendants.has(otherId) && otherId !== leafId) {
          descendants.add(otherId);
          queueDown.push(otherId);
        }
      }
    }
    return { ancestors, descendants };
  }

  function applySpotlight(leafId) {
    const vp = $('forestViewport');
    if (!vp) return;
    if (!leafId) {
      clearSpotlight();
      return;
    }
    spotlightTargetLeafId = leafId;
    vp.classList.add('spotlight-mode');
    const { ancestors, descendants } = findLineage(leafId);

    document.querySelectorAll('.leaf-card').forEach(card => {
      const id = card.dataset.leafId || card.dataset.id;
      card.classList.remove('spotlight-focused', 'spotlight-ancestor', 'spotlight-descendant', 'spotlight-dimmed');
      if (id === leafId) {
        card.classList.add('spotlight-focused');
      } else if (ancestors.has(id)) {
        card.classList.add('spotlight-ancestor');
      } else if (descendants.has(id)) {
        card.classList.add('spotlight-descendant');
      } else {
        card.classList.add('spotlight-dimmed');
      }
    });

    document.querySelectorAll('.branch-node, .canvas-sticky-note').forEach(node => {
      node.classList.add('spotlight-dimmed');
    });

    document.querySelectorAll('.forest-wire').forEach(w => {
      const from = w.dataset.from;
      const to = w.dataset.to;
      const isRelated = (from === leafId && descendants.has(to)) || 
                        (to === leafId && ancestors.has(from)) ||
                        (ancestors.has(from) && ancestors.has(to)) ||
                        (descendants.has(from) && descendants.has(to));
      w.classList.toggle('wire-dimmed', !isRelated);
    });
    toast('已开启血缘探照灯：蓝色为前序依赖，绿色为后续后继。按 Esc 退出。');
  }

  function clearSpotlight() {
    spotlightTargetLeafId = null;
    const vp = $('forestViewport');
    if (vp) vp.classList.remove('spotlight-mode');
    document.querySelectorAll('.spotlight-focused, .spotlight-ancestor, .spotlight-descendant, .spotlight-dimmed').forEach(el => {
      el.classList.remove('spotlight-focused', 'spotlight-ancestor', 'spotlight-descendant', 'spotlight-dimmed');
    });
    document.querySelectorAll('.wire-dimmed').forEach(w => w.classList.remove('wire-dimmed'));
  }

  function toggleSpotlight(leafId) {
    if (spotlightTargetLeafId === leafId) {
      clearSpotlight();
    } else {
      applySpotlight(leafId);
    }
  }

  // --- Section 6.9: Command Palette (全局命令与常用动作面板) ---
  let commandPaletteOpen = false;
  let commandPaletteSelectedIndex = 0;
  let commandPaletteItems = [];
  let lastFocusedElementBeforePalette = null;

  function locateCard(leafId) {
    fitViewport=false;
    if (!leafId) return;
    if (filter !== 'all') {
      const allFilterBtn = document.querySelector('.filter[data-filter="all"]');
      if (allFilterBtn) allFilterBtn.click();
    }
    const searchBox = $('leafSearch');
    if (searchBox && searchBox.value) {
      searchBox.value = '';
      searchQuery = '';
      applyFocus();
    }

    // A located task can be hidden behind several folded ancestor branches.
    // Reveal only its ancestry, keeping other branches and project state intact.
    const leaf = snapshot.forest.leaves.find(node => node.id === leafId);
    const branches = new Map((snapshot.forest.branches || []).map(node => [node.id, node]));
    const visited = new Set();
    let parentId = leaf?.parent_id, expanded = false;
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      if (collapsedBranches.delete(parentId)) expanded = true;
      parentId = branches.get(parentId)?.parent_id;
    }
    if (expanded) { persistCollapsedBranches(); renderForest(); }

    const card = document.getElementById('leaf-' + leafId) || [...document.querySelectorAll('.leaf-card')].find(node => node.dataset.id === leafId || node.dataset.leafId === leafId);
    if (card) {
      const stage = $('forestStage');
      const stageRect = stage.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const vp = $('forestViewport').getBoundingClientRect();

      const cardStageX = parseFloat(card.style.left) || ((cardRect.left - stageRect.left) / (scale || 1));
      const cardStageY = parseFloat(card.style.top) || ((cardRect.top - stageRect.top) / (scale || 1));

      const layer = $('forestPanLayer');
      if (layer) layer.classList.add('focus-motion');

      panX = Math.round(vp.width / 2 - (cardStageX + 155) * scale);
      panY = Math.round(vp.height / 2 - (cardStageY + 50) * scale);
      transform();
      saveViewport();

      setTimeout(() => { if (layer) layer.classList.remove('focus-motion'); }, 420);

      card.classList.remove('locate-pulse');
      void card.offsetWidth;
      card.classList.add('locate-pulse');
      setTimeout(() => card.classList.remove('locate-pulse'), 1800);
      try { card.focus(); } catch (_) {}
    } else {
      toast('未在当前视图中找到该任务节点', true);
    }
  }

  function getCommandPaletteActions() {
    return [
      {
        id: 'action-tidy',
        group: '常用动作',
        icon: '✦',
        title: '智能紧凑布局与排版整理',
        sec: 'Alt+A',
        badge: '布局',
        run: () => { handleTidyUp(); }
      },
      {
        id: 'action-sticky',
        group: '常用动作',
        icon: '📌',
        title: '在画布新建随手记便签',
        sec: 'Alt+T',
        badge: '便签',
        run: () => { createStickyNote(); }
      },
      {
        id: 'action-minimap',
        group: '常用动作',
        icon: '🗺️',
        title: '切换鹰眼全景小地图显隐',
        sec: '',
        badge: '小地图',
        run: () => {
          const btn = $('minimapToggleBtn');
          if (btn) btn.click();
          else toast('小地图已就绪');
        }
      },
      {
        id: 'action-wires',
        group: '常用动作',
        icon: '🔗',
        title: '切换跨任务依赖连线',
        sec: 'Alt+W',
        badge: '依赖',
        run: () => { toggleDependencyWires(); }
      },
      {
        id: 'action-fit',
        group: '常用动作',
        icon: '📐',
        title: '适屏居中：全景显示任务森林',
        sec: 'F',
        badge: '视口',
        run: () => { fit(); }
      },
      {
        id: 'action-reset-zoom',
        group: '常用动作',
        icon: '🔄',
        title: '恢复 1:1 视口默认缩放',
        sec: '0',
        badge: '视口',
        run: () => { reset(); }
      },
      {
        id: 'action-append-task',
        group: '常用动作',
        icon: '➕',
        title: '追加新任务节点',
        sec: 'Alt+N',
        badge: '任务',
        run: () => { showAppendTaskDialog(); }
      },
      {
        id: 'action-theme',
        group: '常用动作',
        icon: '🌓',
        title: '切换深色 / 浅色模式',
        sec: '',
        badge: '界面',
        run: () => { $('themeButton')?.click(); }
      },
      {
        id: 'action-sidebar',
        group: '常用动作',
        icon: '👥',
        title: '收起 / 展开项目成员工位栏',
        sec: 'Ctrl+B',
        badge: '工位',
        run: () => { $('sidebarToggle')?.click(); }
      },
      {
        id: 'action-export-tree',
        group: '常用动作',
        icon: '📂',
        title: '导出当前任务树结构',
        sec: '',
        badge: '数据',
        run: () => { $('exportTreeButton')?.click(); }
      },
      {
        id: 'action-export-handover',
        group: '常用动作',
        icon: '📋',
        title: '导出项目交接总结文档',
        sec: '',
        badge: '数据',
        run: () => { $('exportHandoverButton')?.click(); }
      },
      {
        id: 'action-export-backup',
        group: '常用动作',
        icon: '💾',
        title: '导出项目全局备份',
        sec: '',
        badge: '数据',
        run: () => { $('exportBackupButton')?.click(); }
      },
      {
        id: 'action-export-canvas-image',
        group: '常用动作',
        icon: '🖼️',
        title: '导出整树超清大图',
        sec: '',
        badge: '渲染',
        run: showCanvasExport
      },
      {
        id: 'action-fullscreen',
        group: '常用动作',
        icon: '⛶',
        title: '全屏沉浸画布模式',
        sec: 'F11',
        badge: '视口',
        run: () => { toggleFullscreenCanvas(); }
      },
      {
        id: 'action-quickstart-tour',
        group: '常用动作',
        icon: '🚀',
        title: '新项目初始化向导与上手指南',
        sec: '',
        badge: '帮助',
        run: () => { showQuickstartTour(); }
      },
      {
        id: 'action-restore-backup',
        group: '常用动作',
        icon: '🔄',
        title: '还原项目全局备份',
        sec: '',
        badge: '数据',
        run: () => { $('restoreBackupButton')?.click(); }
      },
      {
        id: 'action-scan-trees',
        group: '常用动作',
        icon: '📊',
        title: '扫描当前项目任务树历史基线',
        sec: '',
        badge: '项目',
        run: () => { scanProjectTrees(); }
      },
      {
        id: 'action-settings',
        group: '常用动作',
        icon: '⚙️',
        title: '工作台与成果复核设置...',
        sec: '',
        badge: '配置',
        run: () => { showSettings(); }
      },
      {
        id: 'action-shortcuts',
        group: '常用动作',
        icon: '⌨️',
        title: '查看桌面快捷键与极客手感手册',
        sec: 'F1 / ?',
        badge: '帮助',
        run: () => { showShortcutsGuide(); }
      },
      {
        id: 'action-refresh',
        group: '常用动作',
        icon: '🔄',
        title: '重新读取本地快照与总线状态',
        sec: 'F5',
        badge: '系统',
        run: () => { refresh(); }
      }
    ];
  }

  function openCommandPalette(initialQuery = '') {
    const backdrop = $('commandPaletteBackdrop');
    const input = $('commandPaletteInput');
    if (!backdrop || !input) return;

    lastFocusedElementBeforePalette = document.activeElement;
    commandPaletteOpen = true;
    backdrop.hidden = false;
    input.value = initialQuery;
    commandPaletteSelectedIndex = 0;
    renderCommandPaletteResults(initialQuery);
    setTimeout(() => { input.focus(); input.select(); }, 20);
  }

  function closeCommandPalette() {
    const backdrop = $('commandPaletteBackdrop');
    if (!backdrop) return;
    backdrop.hidden = true;
    commandPaletteOpen = false;
    if (lastFocusedElementBeforePalette && typeof lastFocusedElementBeforePalette.focus === 'function') {
      try { lastFocusedElementBeforePalette.focus(); } catch (_) {}
    }
  }

  function renderCommandPaletteResults(query = '') {
    const resultsContainer = $('commandPaletteResults');
    const input = $('commandPaletteInput');
    if (!resultsContainer) return;

    resultsContainer.innerHTML = '';
    const q = (query || '').trim().toLowerCase();

    const allActions = getCommandPaletteActions();
    const filteredActions = allActions.filter(act => {
      if (!q) return true;
      return act.title.toLowerCase().includes(q) ||
             (act.sec && act.sec.toLowerCase().includes(q)) ||
             (act.badge && act.badge.toLowerCase().includes(q));
    });

    const leaves = snapshot?.forest?.leaves || [];
    const filteredLeaves = leaves.filter(leaf => {
      if (!q) return false;
      const sec = (leaf.section_no || '').toLowerCase();
      const title = (leaf.title || '').toLowerCase();
      const goal = (leaf.goal || '').toLowerCase();
      const id = (leaf.id || '').toLowerCase();
      return sec.includes(q) || title.includes(q) || goal.includes(q) || id.includes(q);
    }).slice(0, 8);

    const members = snapshot?.group?.members || [];
    const filteredMembers = members.filter(m => {
      if (!q) return false;
      return m.toLowerCase().includes(q);
    });

    commandPaletteItems = [];
    let currentIndex = 0;

    function appendGroup(groupTitle, items) {
      if (items.length === 0) return;
      const titleEl = el('div', 'palette-group-title', groupTitle);
      resultsContainer.appendChild(titleEl);

      items.forEach(item => {
        const itemIndex = currentIndex++;
        const itemEl = el('div', 'palette-item', '');
        itemEl.id = 'palette-opt-' + itemIndex;
        itemEl.setAttribute('role', 'option');
        itemEl.setAttribute('aria-selected', itemIndex === commandPaletteSelectedIndex ? 'true' : 'false');
        if (itemIndex === commandPaletteSelectedIndex) {
          itemEl.classList.add('active');
        }

        const leftEl = el('div', 'palette-item-left');
        const iconEl = el('span', 'palette-item-icon', item.icon || '📄');
        leftEl.appendChild(iconEl);

        if (item.secBadge) {
          const secBadge = el('span', 'palette-item-sec', item.secBadge);
          leftEl.appendChild(secBadge);
        }

        const titleSpan = el('span', 'palette-item-title', item.title);
        leftEl.appendChild(titleSpan);
        itemEl.appendChild(leftEl);

        if (item.badge) {
          const badgeEl = el('span', 'palette-item-badge', item.badge);
          itemEl.appendChild(badgeEl);
        } else if (item.shortcut) {
          const scEl = el('kbd', 'palette-item-shortcut', item.shortcut);
          itemEl.appendChild(scEl);
        }

        itemEl.addEventListener('pointerenter', () => {
          commandPaletteSelectedIndex = itemIndex;
          updateActivePaletteItem();
        });

        itemEl.addEventListener('click', () => {
          executePaletteItem(itemIndex);
        });

        resultsContainer.appendChild(itemEl);
        commandPaletteItems.push({ ...item, element: itemEl });
      });
    }

    appendGroup('常用动作', filteredActions.map(a => ({
      ...a,
      shortcut: a.sec,
      execute: () => a.run()
    })));

    if (filteredLeaves.length > 0) {
      appendGroup('任务节点与章节', filteredLeaves.map(leaf => {
        const stateLabels = {
          delivered: '已交付',
          running: '执行中',
          claimed: '已认领',
          blocked: '已阻塞',
          queued: '待排队'
        };
        const stateLabel = stateLabels[leaf.state] || '待认领';
        return {
          id: 'leaf-' + leaf.id,
          icon: leaf.state === 'delivered' ? '✓' : '◈',
          secBadge: leaf.section_no ? '§' + leaf.section_no : '',
          title: leaf.title || leaf.id,
          badge: stateLabel,
          execute: () => { locateCard(leaf.id); }
        };
      }));
    }

    if (filteredMembers.length > 0) {
      appendGroup('协作工位', filteredMembers.map(m => ({
        id: 'member-' + m,
        icon: '👤',
        title: '成员工位：' + m,
        badge: '聚焦筛选',
        execute: () => {
          const item = [...document.querySelectorAll('.member-item')].find(node => node.dataset.member === m);
          if (item) item.click();
          else toast('已切换至成员: ' + m);
        }
      })));
    }

    if (commandPaletteItems.length === 0) {
      resultsContainer.appendChild(el('div', 'palette-empty', '未找到匹配的动作、章节或成员'));
      input.removeAttribute('aria-activedescendant');
    } else {
      if (commandPaletteSelectedIndex >= commandPaletteItems.length) {
        commandPaletteSelectedIndex = 0;
      }
      updateActivePaletteItem();
    }
  }

  function updateActivePaletteItem() {
    const input = $('commandPaletteInput');
    commandPaletteItems.forEach((it, idx) => {
      const active = (idx === commandPaletteSelectedIndex);
      it.element.classList.toggle('active', active);
      it.element.setAttribute('aria-selected', active ? 'true' : 'false');
      if (active) {
        input?.setAttribute('aria-activedescendant', it.element.id);
        it.element.scrollIntoView({ block: 'nearest' });
      }
    });
  }

  function executePaletteItem(index) {
    const item = commandPaletteItems[index];
    if (item && typeof item.execute === 'function') {
      closeCommandPalette();
      try {
        item.execute();
      } catch (err) {
        toast('执行失败: ' + err.message, true);
      }
    }
  }

  function initCommandPalette() {
    const backdrop = $('commandPaletteBackdrop');
    const input = $('commandPaletteInput');
    const quickBtn = $('quickCommandPaletteBtn');
    const menuAction = $('menuCommandPaletteAction');

    if (quickBtn) quickBtn.addEventListener('click', () => openCommandPalette());
    if (menuAction) menuAction.addEventListener('click', () => openCommandPalette());

    if (backdrop) {
      backdrop.addEventListener('click', (ev) => {
        if (ev.target === backdrop) closeCommandPalette();
      });
    }

    if (input) {
      input.addEventListener('input', (ev) => {
        commandPaletteSelectedIndex = 0;
        renderCommandPaletteResults(ev.target.value);
      });

      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'ArrowDown') {
          ev.preventDefault();
          if (commandPaletteItems.length > 0) {
            commandPaletteSelectedIndex = (commandPaletteSelectedIndex + 1) % commandPaletteItems.length;
            updateActivePaletteItem();
          }
        } else if (ev.key === 'ArrowUp') {
          ev.preventDefault();
          if (commandPaletteItems.length > 0) {
            commandPaletteSelectedIndex = (commandPaletteSelectedIndex - 1 + commandPaletteItems.length) % commandPaletteItems.length;
            updateActivePaletteItem();
          }
        } else if (ev.key === 'Enter') {
          ev.preventDefault();
          if (commandPaletteItems.length > 0) {
            executePaletteItem(commandPaletteSelectedIndex);
          }
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          closeCommandPalette();
        }
      });
    }
  }

  window.AgentBusUI={setSnapshot,inspect,refresh,fit,call,callTimeoutMs:DEFAULT_CALL_TIMEOUT_MS,mutationGeneration:()=>mutationGeneration,advanceMutationGeneration:()=>++mutationGeneration,isMutatingAction,lastRejectedSnapshot:()=>lastRejectedSnapshot,workspaceDropped,undo:performUndo,redo:performRedo,undoStack,redoStack,batchSelection:{toggle:toggleCardBatchSelected,clear:clearBatchSelection,getSelected:()=>[...batchSelectedCardIds]},cloneCard:executeCloneCard,inlineRename:startInPlaceCardRename,workspaceDropRejected:()=>toast('请拖入一个现有的本地项目文件夹。',true),sticky:{create:createStickyNote,load:loadStickyNotes,render:renderStickyNotes,remove:removeStickyNote,colors:stickyColorIds},minimap:{update:updateMinimap,getBounds:getMinimapWorldBounds,navigate:navigateFromMinimap,isCollapsed:()=>Boolean($('canvasMinimap')?.classList.contains('collapsed'))},commandPalette:{open:openCommandPalette,close:closeCommandPalette,isOpen:()=>commandPaletteOpen,locate:locateCard,getActions:getCommandPaletteActions},exportCanvasImage,toggleFullscreen:toggleFullscreenCanvas,showQuickstartTour,playCrispDeliveryTone};installStickyToolbarButton();renderStickyNotes();initMinimap();initCommandPalette();setConnection(bridge());refresh();
  setInterval(()=>{if(bridge()&&!document.hidden&&!$('workspaceDialog')?.open&&!document.querySelector('.in-place-create-card')&&!commandPaletteOpen)refresh();},4000);
  if (bridge()) startAutomaticReleaseChecks();
  else window.addEventListener('pywebviewready', startAutomaticReleaseChecks, {once:true});
})();
