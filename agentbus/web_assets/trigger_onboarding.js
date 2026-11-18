/**
 * AgentBus Trigger & Onboarding Wizard (S0 - S6)
 * Document: 02-新成员接入向导与产品流程.md
 */

(function () {
  'use strict';

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  async function callBridge(action, payload) {
    if (typeof window.agentbusCall === 'function') {
      return await window.agentbusCall(action, payload);
    }
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.call === 'function') {
      const res = await window.pywebview.api.call(action, payload);
      if (!res || res.ok !== true) {
        throw new Error((res && res.error) || '桌面调用失败');
      }
      return res.data;
    }
    throw new Error('未连接桌面运行环境，请在 AgentBus 桌面窗口中操作。');
  }

  class TriggerOnboardingWizard {
    constructor(containerId, options = {}) {
      this.container = typeof containerId === 'string' ? document.getElementById(containerId) : containerId;
      this.options = options;
      this.flowState = null;
      this.currentStep = 'S0';
      this.verificationPlan = null;
      this.verificationReceipt = null;
      this.busy = false;
      this.transitionPending = false;
    }

    async init(memberId) {
      this.memberId = memberId;
      await this.startFlow();
      this.render();
    }

    async startFlow() {
      try {
        this.flowState = await callBridge('trigger_onboarding_start', { member_id: this.memberId });
        this.currentStep = this.flowState.step || 'S0';
      } catch (err) {
        console.error('Failed to start onboarding flow:', err);
        if (this.container) {
          this.container.innerHTML = `<div class="error-note">无法启动接入向导: ${escapeHtml(err.message)}</div>`;
        }
      }
    }

    async advance(action, payload = {}) {
      if (this.busy) return;
      this.busy = true;
      this.renderLoading('正在处理...');
      try {
        const nextState = await callBridge('trigger_onboarding_advance', {
          flow_id: this.flowState.flow_id,
          expected_revision: this.flowState.revision,
          action,
          payload,
        });
        this.flowState = nextState;
        this.currentStep = this.flowState.step;
        this.render();
      } catch (err) {
        alert(`操作失败: ${err.message}`);
        this.render();
      } finally {
        this.busy = false;
      }
    }

    renderLoading(msg) {
      if (!this.container) return;
      this.container.innerHTML = `<div class="loading-copy">${msg}</div>`;
    }

    render() {
      if (!this.container || !this.flowState) return;
      const stepNames = {
        S0: 'S0: 确认项目与成员身份',
        S1: 'S1: 选择软件与环境探测',
        S2: 'S2: 确认支持范围与连接方式',
        S3: 'S3: 安装或激活连接组件',
        S4: 'S4: 绑定目标桌面会话',
        S5: 'S5: 有界受控往返验证',
        S6: 'S6: 启用本项目自动协作',
        COMPLETED: '接入完成 · 已就绪',
      };

      const title = stepNames[this.currentStep] || this.currentStep;
      this.container.innerHTML = `
        <div class="agentbus-onboarding-card" style="padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px;">
          <div class="wizard-header" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid rgba(255,255,255,0.1); padding-bottom:8px;">
            <h3 style="margin:0; font-size:16px;">${escapeHtml(title)}</h3>
            <span class="badge" style="background:var(--accent,#4daafc); color:#000; padding:2px 8px; border-radius:10px; font-size:12px; font-weight:bold;">${escapeHtml(this.flowState.member_id)}</span>
          </div>
          <div class="wizard-body">
            ${this.renderStepContent()}
          </div>
        </div>
      `;
      this.container.querySelectorAll('[data-wizard-action]').forEach(button => {
        button.addEventListener('click', () => this.handleAction(button));
      });
    }

    async handleAction(button) {
      if (this.busy || this.transitionPending || button.disabled) return;
      const actions = {
        confirm_identity: () => this.advance('confirm_identity', {}),
        choose_file_collaboration: () => this.advance('choose_file_collaboration', {}),
        select_software: () => this.handleSelectSoftware(),
        confirm_scope: () => this.advance('confirm_scope', {connector_id: button.dataset.connectorId}),
        apply_install: () => this.advance('apply_install', {}),
        copy_checkin: async () => {
          const command = this.container.querySelector('#checkin_cmd_box')?.value;
          if (!command) throw new Error('尚无可复制的报到命令，请核对项目入口。');
          await navigator.clipboard.writeText(command);
          const tip = this.container.querySelector('#checkin_status_tip');
          if (tip) tip.textContent = '报到命令已复制。';
        },
        check_bound: () => this.checkAutoBound(),
        bind_chat: () => this.handleBindChat(),
        run_verification: () => this.handleRunVerification(),
        enable_collaboration: () => this.advance('enable_collaboration', {auto_wake: true}),
        close: () => { if (window.closeDialog) window.closeDialog(); },
      };
      const run = actions[button.dataset.wizardAction];
      if (!run) return;
      button.disabled = true;
      try {
        await run();
      } catch (error) {
        alert('操作未完成：' + (error.message || error));
      } finally {
        button.disabled = this.transitionPending;
      }
    }

    renderStepContent() {
      switch (this.currentStep) {
        case 'S0':
          return `
            <p>为成员 <strong>${escapeHtml(this.flowState.member_id)}</strong> 配置本项目桌面会话直连与自动触发唤醒。</p>
            <p style="font-size:13px; color:rgba(255,255,255,0.6);">当前工程：<code>${escapeHtml(this.flowState.project_key)}</code></p>
            <div style="margin-top:16px; display:flex; gap:10px;">
              <button class="button primary small" data-wizard-action="confirm_identity">下一步：选择宿主软件</button>
              <button class="button small" data-wizard-action="choose_file_collaboration">仅加入文件协作（不绑定聊天）</button>
            </div>
          `;
        case 'S1':
          return `
            <p>选择该成员运行的客户端软件环境：</p>
            <div style="margin: 12px 0; display:flex; flex-direction:column; gap:8px;">
              <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                <input type="radio" name="sw" value="codearts" checked />
                <span><strong>华为 CodeArts Agent</strong> (项目 Hook；宿主是否接收需实际验证)</span>
              </label>
              <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                <input type="radio" name="sw" value="codex" />
                <span><strong>OpenAI Codex</strong> (Bridge 接入；以当前宿主能力与真实回执为准)</span>
              </label>
              <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                <input type="radio" name="sw" value="acp" />
                <span><strong>ACP 标准协议客户端</strong> (会话加载与发送能力由实际客户端决定)</span>
              </label>
              <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                <input type="radio" name="sw" value="other" />
                <span><strong>其他新软件 / 通用 Agent</strong> (文件与本地信号唤醒；需验证宿主是否实际执行)</span>
              </label>
            </div>
            <div style="margin-top:16px; display:flex; gap:10px;">
              <button class="button primary small" data-wizard-action="select_software">下一步：确认支持范围</button>
            </div>
          `;
        case 'S2':
          const prod = this.flowState.selected_product || 'codearts';
          const connMap = {
            codearts: 'agentbus.codearts-hook',
            codex: 'agentbus.codex-bridge',
            acp: 'agentbus.acp-bridge',
          };
          const connId = connMap[prod] || 'agentbus.universal-wakeup';
          return `
            <p>已识别宿主软件：<strong>${escapeHtml(prod)}</strong></p>
            <p style="font-size:13px; color:rgba(255,255,255,0.7);">推荐适配连接器：<code>${connId}</code></p>
            <p style="font-size:12px; color:rgba(255,255,255,0.5);">该连接器将在本地受控工作区下工作，不修改外部厂商安装目录，不访问敏感路径。</p>
            <div style="margin-top:16px; display:flex; gap:10px;">
              <button class="button primary small" data-wizard-action="confirm_scope" data-connector-id="${connId}">确认并进入组件部署</button>
            </div>
          `;
        case 'S3':
          return `
            <p>将向当前工程安装适配配置与一次性接入邀请。</p>
            <p style="font-size:12px; color:rgba(255,255,255,0.6);">包含：<code>.codearts/hooks/agentbus.js</code>（若选择 CodeArts）与 20分钟一次性接入凭证。</p>
            <div style="margin-top:16px; display:flex; gap:10px;">
              <button class="button primary small" data-wizard-action="apply_install">部署组件并生成接入邀请</button>
            </div>
          `;
        case 'S4':
          const checkinCmd = this.flowState.checkin_command || '';
          return `
            <p>一次性接入邀请：<code style="color:var(--accent,#4daafc);">${escapeHtml(this.flowState.invite_id || '已就绪')}</code></p>
            <div style="background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:6px; padding:12px; margin:12px 0;">
              <div style="font-weight:bold; font-size:13px; color:#7ee787; margin-bottom:6px;">✨ 方式 A：Agent 按当前会话信息报到</div>
              <div style="font-size:12px; color:rgba(255,255,255,0.7); margin-bottom:8px;">
                在目标 Agent 中取得当前宿主真实会话 ID，替换命令中的占位文字后执行。命令明确指定本项目、成员与连接器；报到记录仍需实际任务验证：
              </div>
              <div style="display:flex; gap:8px; align-items:center;">
                <input type="text" id="checkin_cmd_box" readonly class="field" style="width:100%; font-family:monospace; font-size:12px;" value="${escapeHtml(checkinCmd)}" placeholder="尚未取得正式程序登记，请核对项目入口" />
                <button class="button small" data-wizard-action="copy_checkin">复制</button>
              </div>
              <div style="margin-top:10px; display:flex; gap:10px; align-items:center;">
                <button class="button primary small" data-wizard-action="check_bound">已发命令，检测报到状态</button>
                <span id="checkin_status_tip" style="font-size:12px; color:rgba(255,255,255,0.6);"></span>
              </div>
            </div>
            <details style="margin-top:12px; font-size:12px;">
              <summary style="cursor:pointer; color:rgba(255,255,255,0.5);">高级选项：手动指定会话标识 (Debug 模式)</summary>
              <div style="padding-top:8px;">
                <label style="display:block; margin-bottom:4px;">本地会话标识（Native Conversation ID）：</label>
                <input type="text" id="target_sess_id" class="field" style="width:100%; box-sizing:border-box;" value="" placeholder="填写宿主提供的真实会话 ID" />
                <div style="margin-top:8px;">
                  <button class="button small" data-wizard-action="bind_chat">手动确认绑定</button>
                </div>
              </div>
            </details>
          `;
        case 'S5':
          return `
            <p>联系卡已完成初步绑定。启动后会发布一个正式算题任务；只有成员实际认领、执行并交付匹配的产物与回执后，验证才会通过。</p>
            <div id="verificationBox" style="margin: 12px 0; padding:10px; background:rgba(0,0,0,0.2); border-radius:4px; font-size:13px;">
              <span>点击下方按钮运行受控往返验证：</span>
            </div>
            <div style="margin-top:16px; display:flex; gap:10px;">
              <button class="button primary small" id="btnRunVerification" data-wizard-action="run_verification">${this.verificationReceipt?.run_id ? '检查已有验证回执' : '发布一次有界算题任务'}</button>
            </div>
          `;
        case 'S6':
          return `
            <p style="color:#7ee787; font-weight:bold;">正式样例任务的成员交付与产物核验已通过。</p>
            <p>可为本项目启用唤醒。宿主实际接收、后续任务执行与长期运行状态仍以各次真实回执为准。</p>
            <div style="margin-top:16px; display:flex; gap:10px;">
              <button class="button primary small" style="background:#238636;" data-wizard-action="enable_collaboration">启用本项目自动唤醒</button>
            </div>
          `;
        case 'COMPLETED':
          return `
            <div style="text-align:center; padding:16px 0;">
              <h4 style="color:#7ee787; margin-bottom:8px;">接入成功</h4>
              <p>成员 <strong>${escapeHtml(this.flowState.member_id)}</strong> 的接入设置已保存。实际执行状态请查看任务与回执。</p>
              <button class="button small" data-wizard-action="close">完成并关闭</button>
            </div>
          `;
        default:
          return `<p>当前处于步骤: <strong>${escapeHtml(this.currentStep)}</strong></p>`;
      }
    }

    handleSelectSoftware() {
      const selected = this.container.querySelector('input[name="sw"]:checked')?.value || 'codearts';
      return this.advance('select_software', { product_id: selected, installation_key: `inst_${selected}_01` });
    }

    handleBindChat() {
      const val = this.container.querySelector('#target_sess_id')?.value?.trim();
      if (!val) { alert('请填写宿主提供的真实会话 ID。'); return; }
      return this.advance('bind_chat', { native_conversation_id: val, identity_source: 'user_confirmed' });
    }

    async checkAutoBound() {
      const tip = this.container.querySelector('#checkin_status_tip');
      if (tip) tip.innerText = '正在核验成员报到联系卡...';
      try {
        const contact = await callBridge('contact_show', { agent: this.flowState.member_id });
        if (contact && contact.status === 'bound' && contact.native_conversation_id
            && contact.connector_id === this.flowState.connector_id
            && contact.installation_key === this.flowState.selected_installation) {
          if (tip) tip.innerText = '✅ 检测到已成功自主报到！正在进入下一步...';
          this.transitionPending = true;
          setTimeout(() => {
            this.transitionPending = false;
            this.advance('bind_chat', {
              native_conversation_id: contact.native_conversation_id,
              identity_source: 'self_checkin'
            });
          }, 800);
        } else {
          if (tip) tip.innerText = '⚠️ 暂未检测到有效联系卡，请先在 Agent 会话中运行报到命令。';
        }
      } catch (e) {
        if (tip) tip.innerText = '⚠️ 检测联系卡状态失败：' + (e.message || e);
      }
    }

    async handleRunVerification() {
      const box = this.container.querySelector('#verificationBox');
      const btn = this.container.querySelector('#btnRunVerification');
      if (btn) btn.disabled = true;
      if (box) box.innerHTML = '<span class="loading-copy">正在查询正式验证任务与回执...</span>';
      try {
        let receipt;
        if (this.verificationReceipt?.run_id) {
          receipt = await callBridge('trigger_verification_check', { run_id: this.verificationReceipt.run_id });
        } else {
          if (!this.verificationPlan) {
            this.verificationPlan = await callBridge('trigger_verification_plan', { member_id: this.memberId });
          }
          receipt = await callBridge('trigger_verification_run', { plan: this.verificationPlan });
        }
        this.verificationReceipt = receipt;
        if (receipt && receipt.passed && receipt.status === 'passed') {
          if (box) box.innerHTML = '<span style="color:#7ee787;">[验证通过] 正式成员回执与产物核验一致。</span>';
          this.transitionPending = true;
          setTimeout(() => {
            this.transitionPending = false;
            this.advance('complete_verification', { run_id: receipt.run_id });
          }, 800);
        } else {
          const pending = receipt && ['pending', 'in_progress'].includes(receipt.status);
          if (box) box.innerHTML = `<span style="color:${pending ? '#d29922' : '#f85149'};">[${pending ? '等待成员实际执行' : '验证未通过'}] ${escapeHtml(receipt?.error_message || receipt?.reason_code || '尚无有效正式回执')}</span>`;
          if (btn) btn.textContent = '检查已有验证回执';
          if (btn) btn.disabled = false;
        }
      } catch (err) {
          if (box) box.innerHTML = `<span style="color:#f85149;">验证执行异常: ${escapeHtml(err.message)}</span>`;
        if (btn) btn.disabled = false;
      }
    }
  }

  window.TriggerOnboardingWizard = TriggerOnboardingWizard;
})();
