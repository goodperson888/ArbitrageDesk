/* Read-only diagrams and staged channel settings; never starts execution. */
(() => {
    'use strict';
    const defaults={method:'quantile',window_seconds:3600,quantile:90,multiplier:2,min_samples:30,
        min_width_bps:0,max_width_bps:0,lower_bps:-20,upper_bps:20,exit_mode:'reversion',reversion_percent:60,
        touch_tolerance_bps:2,touch_min_count:3};
    const fields={method:'channel-method',window_seconds:'channel-window',quantile:'channel-quantile',
        multiplier:'channel-multiplier',min_samples:'channel-min-samples',min_width_bps:'channel-min-width',
        max_width_bps:'channel-max-width',lower_bps:'channel-lower',upper_bps:'channel-upper',
        exit_mode:'channel-exit-mode',reversion_percent:'channel-reversion',
        touch_tolerance_bps:'channel-touch-tolerance',touch_min_count:'channel-touch-count'};
    const methods={quantile:'历史分位',touches:'反复触碰',bollinger:'布林带',robust:'稳健通道',fixed:'固定区间'};
    const names={arcus:'Arcus',lighter:'Lighter 主网',lighter_rh:'Lighter RH',standx:'StandX'};
    const get=id=>document.getElementById(id);
    const finite=value=>typeof value==='number'&&Number.isFinite(value);
    let saved={},viewConfig={},frozenPosition=null,beforeEdit={},readOnly=true,saving=false,tab='rule',chart=null,preview=null,timer=null,requestId=0,controller=null,lastFocus=null;
    function read(){return Object.fromEntries(Object.entries(fields).map(([key,id])=>[key,
        ['method','exit_mode'].includes(key)?(get(id)?.value||defaults[key]):Number(get(id)?.value??defaults[key])]));}
    function fill(policy){for(const [key,id]of Object.entries(fields)){if(get(id))get(id).value=(policy||{})[key]??defaults[key];}visibility();}
    function validate(p=read()){
        if(p.lower_bps>=p.upper_bps)return '下边界须小于上边界';
        if(p.max_width_bps>0&&p.max_width_bps<p.min_width_bps)return '最大宽度不能小于最小宽度';
        if(Object.values(p).some(v=>typeof v==='number'&&!Number.isFinite(v)))return '请填写有效的通道参数';
        return '';
    }
    function visibility(){
        const p=read();
        document.querySelectorAll('[data-channel-kind]').forEach(node=>{node.hidden=!node.dataset.channelKind.split(' ').includes(p.method);});
        document.querySelectorAll('[data-channel-stat]').forEach(node=>{node.hidden=p.method==='fixed';});
        if(get('channel-reversion-field'))get('channel-reversion-field').hidden=p.exit_mode!=='reversion';
    }
    function summary(){
        const active=saved.entry_policy?.mode==='channel',p={...defaults,...saved.channel_policy};
        const text=active?`${methods[p.method]} · ${p.exit_mode==='opposite'?'对侧循环':`回归 ${p.reversion_percent}% 且盈利退出`}`:
            saved.entry_policy?.mode==='adaptive_quantile'?'历史分位 + 整轮净收益门槛':'净回归空间门槛';
        if(get('strategy-quick-summary'))get('strategy-quick-summary').textContent='已保存策略：'+text;
        if(get('channel-settings-summary'))get('channel-settings-summary').textContent=active?text:'可选通道策略 · 当前未启用';
    }
    function load(config){saved=config||{};if(!get('strategy-preview-dialog')?.open)fill(saved.channel_policy);summary();}
    function ruleData(p,bounds){
        const b=bounds||{upper_bps:p.method==='fixed'?p.upper_bps:20,lower_bps:p.method==='fixed'?p.lower_bps:-20};
        const c=finite(b.center_bps)?b.center_bps:(b.upper_bps+b.lower_bps)/2;
        const upper=b.upper_bps,lower=b.lower_bps,span=Math.max(upper-lower,1);
        const up=upper+span*.12,down=lower-span*.12;
        const u=p.exit_mode==='opposite'?lower:up+(c-up)*p.reversion_percent/100;
        const d=p.exit_mode==='opposite'?upper:down+(c-down)*p.reversion_percent/100;
        const values=[c,c+span*.15,upper,up,up-span*.08,u,c,lower,down,down+span*.08,d,c];
        return {values,upper,lower,center:c,upperExit:u,lowerExit:d};
    }
    function draw(){
        const root=get('strategy-preview-chart');if(!root)return;
        if(!window.echarts){root.textContent='图表资源暂不可用；下方文字规则仍可查看。';return;}
        chart=chart||window.echarts.init(root,null,{renderer:'canvas'});
        const config=readOnly?viewConfig:saved;
        const p=read(),channel=!readOnly||config.entry_policy?.mode==='channel';
        const frozen=frozenPosition?.entry_target?.channel;
        const rule=ruleData(p,frozen||preview?.bounds),historical=tab==='history',points=preview?.points||[];
        if(frozen)rule.values=[frozen.entry_axis_bps,
            frozen.entry_axis_bps+(frozen.side==='upper'?1:-1)*(rule.upper-rule.lower)*.12,
            (frozen.entry_axis_bps+frozen.exit_axis_bps)/2,frozen.exit_axis_bps,rule.center];
        const lines=[];
        const add=(name,value,color,type='dashed')=>{if(finite(value))lines.push({name,yAxis:value,lineStyle:{color,type},label:{position:'insideEndTop',fontSize:11,formatter:()=>`${name} ${value.toFixed(2)} bps`,color}});};
        if(channel){add('上边界',rule.upper,'#e9ad70');add('中心',rule.center,'#adbdd0','solid');add('下边界',rule.lower,'#8ca6ff');}
        else if(!frozenPosition){
            const mode=config.close_policy?.target_mode||'center';
            if(mode!=='center')add('零目标',0,'#adbdd0','solid');
            if(mode!=='zero')add(historical?'当前历史中心':'回归目标示意',historical?preview?.statistics?.center_bps:rule.center,'#adbdd0','solid');
        }
        if(frozen){add('本单入场',frozen.entry_axis_bps,'#e9ad70','solid');add('本单冻结平仓目标',frozen.exit_axis_bps,'#65e1c0','solid');}
        for(const pos of (historical?preview?.positions||[]:[]).slice(0,6)){
            const ch=pos.entry_target?.channel;if(ch)add('持仓冻结目标',ch.exit_axis_bps,'#65e1c0','solid');
        }
        const marks=channel&&!historical&&!frozen?[{name:'上侧入场\nA 多 / B 空',coord:[3,rule.values[3]],itemStyle:{color:'#e9ad70'}},
            {name:'平仓候选\n仍需净盈利',coord:[5,rule.upperExit],itemStyle:{color:'#65e1c0'}},
            {name:'下侧入场\nA 空 / B 多',coord:[8,rule.values[8]],itemStyle:{color:'#8ca6ff'}},
            {name:'平仓候选\n仍需净盈利',coord:[10,rule.lowerExit],itemStyle:{color:'#65e1c0'}}]:[];
        const touches=(frozen||preview?.bounds)?.touch_statistics;
        const zones=touches?['upper','lower'].filter(side=>touches[side]).map(side=>[
            {name:(side==='upper'?'上沿':'下沿')+' · '+touches[side].count+' 次独立触碰',
                yAxis:touches[side].low_bps,itemStyle:{color:side==='upper'?'#e9ad7020':'#8ca6ff20'}},
            {yAxis:touches[side].high_bps}]):[];
        chart.setOption({animation:false,backgroundColor:'transparent',grid:{left:root.clientWidth<500?48:65,right:20,top:48,bottom:45},
            tooltip:{trigger:'axis',valueFormatter:v=>finite(v)?v.toFixed(3)+' bps':v},
            xAxis:historical?{type:'time',splitNumber:4,axisLabel:{color:'#a8b8ca',hideOverlap:true}}:{type:'category',data:rule.values.map((_,i)=>i+1),name:'示意过程',nameLocation:'middle',nameGap:20,axisLabel:{show:false}},
            yAxis:{type:'value',scale:true,name:'价差 / bps',nameTextStyle:{color:'#a8b8ca'},axisLabel:{color:'#a8b8ca'},splitLine:{lineStyle:{color:'#263347'}}},
            series:[{type:'line',name:historical?'历史价差':'规则示意（非真实行情）',showSymbol:false,lineStyle:{color:'#55d9ba',width:2},
                data:historical?points.map(p=>[p.timestamp*1000,p.value_bps]):rule.values,
                markArea:{silent:true,data:zones},
                markLine:{silent:true,symbol:'none',data:lines},markPoint:{symbol:'circle',symbolSize:10,label:{show:true,formatter:'{b}',color:'#e6eef8',position:'top',fontSize:11},data:marks}},
                ...(historical&&touches?['upper','lower'].filter(side=>touches[side]).map(side=>({
                    type:'scatter',name:(side==='upper'?'上沿':'下沿')+'独立触碰',symbolSize:7,
                    itemStyle:{color:side==='upper'?'#e9ad70':'#8ca6ff'},
                    data:touches[side].points.map(p=>[p.timestamp*1000,p.value_bps])})):[])]},true);
        const desc=channel?`${p.exit_mode==='opposite'?'到冻结对侧边界且净盈利后并发平仓；双边确认结束，再核验最新反向机会。':`入场后向冻结中心回归 ${p.reversion_percent}%，并且实际可成交净收益达标才正常平仓。`} 上边界 A 多 B 空，下边界 A 空 B 多。`:
            `当前使用${config.entry_policy?.mode==='adaptive_quantile'?'历史分位加':''}净回归空间门槛。正常退出按已保存的平仓触发方式，核验实际净收益。`;
        const close=config.close_policy||{};
        if(frozenPosition&&!frozen){
            get('strategy-preview-description').textContent='旧仓冻结方向目标 '+(frozenPosition.entry_target?.target_bps??'未知')+' bps；方向价差图请查看订单详情。旧记录缺完整保护参数，无法还原全部设置。';
            return;
        }
        get('strategy-preview-description').textContent=(channel&&p.method==='touches'
            ? `上下沿各需 ${p.touch_min_count} 次独立触碰；容差 ${p.touch_tolerance_bps} bps。${p.window_seconds>604800?'触碰只取最近 7 天原始转折。':''}`:'')+
            (historical?preview?.chart_note||'等待本地历史。':'假设走势示意，触边仍需执行核验。 ')+desc+
            ` 最低平仓净盈利 ${close.min_profit_usdc??0} USDC / ${close.min_profit_bps??0} bps，另加成本预留 ${close.cost_reserve_usdc??0} USDC。`+
            (close.max_hold_seconds?` 最长持仓 ${close.max_hold_seconds/60} 分钟触发保护，可能亏损。`:' 未设置最长持仓时限。')+
            (config.risk?.max_loss_per_trade!=null?` 单笔亏损保护 ${config.risk.max_loss_per_trade} USDC，可能亏损退出。`:' 未设置固定金额止损。')+
            (frozen?' 本单目标保持入场冻结值。':'');
    }
    function positions(){
        const root=get('strategy-preview-positions');root.replaceChildren();
        for(const position of frozenPosition?[frozenPosition]:(preview?.positions||[])){
            const node=document.createElement('p'),ch=position.entry_target?.channel;
            node.textContent=ch?`${position.symbol} · ${position.status} · 持仓冻结：${methods[ch.method]||ch.method}；入场 ${ch.entry_axis_bps.toFixed(2)} → 平仓目标 ${ch.exit_axis_bps.toFixed(2)} bps；保存新设置不会移动此目标。`:
                `${position.symbol} · ${position.status} · 该仓沿用原策略，冻结方向价差目标 ${position.entry_target?.target_bps??'旧版目标'} bps；与当前通道预览分开。`;
            root.appendChild(node);
        }
    }
    async function refresh(){
        if(!get('strategy-preview-dialog')?.open)return;
        visibility();const error=validate();get('strategy-preview-error').textContent=error;if(error){draw();return;}
        const id=++requestId;controller?.abort();controller=new AbortController();
        if(!frozenPosition){
            preview=null;
            get('strategy-preview-axis').textContent='全局策略 · 各品种与平台组合独立计算通道。';
            get('strategy-preview-status').textContent=readOnly?'已保存策略 · 规则示意':'规则示意 · 参数适用于全局执行范围';
            positions();draw();return;
        }
        const venues=(frozenPosition.entry_target?.channel?.axis_venues||
            [frozenPosition.variational_venue,frozenPosition.lighter_venue]).slice().sort();
        if(venues.length!==2||!venues[0]||!venues[1]||venues[0]===venues[1]){get('strategy-preview-status').textContent='本单缺少可核对的平台组合。';preview=null;draw();return;}
        get('strategy-preview-axis').textContent=`${frozenPosition.symbol} · A = ${names[venues[0]]||venues[0]}，B = ${names[venues[1]]||venues[1]} · 本单历史与冻结目标`;
        get('strategy-preview-status').textContent='正在读取本地历史…';
        try{
            const response=await fetch('/api/strategy-preview',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]')?.content||''},
                body:JSON.stringify({symbol:frozenPosition.symbol,venues,channel_policy:read()}),signal:controller.signal});
            const data=await response.json();if(id!==requestId||!get('strategy-preview-dialog').open)return;
            if(!response.ok)throw new Error(data.error||'预览读取失败');preview=data;
            get('strategy-preview-status').textContent=frozenPosition?'本单冻结策略 · 历史折线仅供对照':readOnly&&viewConfig.entry_policy?.mode!=='channel'?'已保存策略 · 通道参数未启用':data.error||`通道就绪 · ${data.bounds?.samples||0} 个等时间采样 · 总宽 ${data.bounds?.width_bps?.toFixed(2)||'--'} bps`;
            if(!data.points?.length&&tab==='history')get('strategy-preview-status').textContent+=' · 所选组合暂无可绘制历史';
            positions();draw();
        }catch(error){if(error.name==='AbortError'||id!==requestId)return;preview=null;get('strategy-preview-status').textContent=error.message;draw();}
    }
    function schedule(){clearTimeout(timer);visibility();preview=null;draw();timer=setTimeout(refresh,350);}
    function open(edit=false,position=null){
        const dialog=get('strategy-preview-dialog');if(!dialog)return;lastFocus=document.activeElement;readOnly=!edit;beforeEdit=read();
        frozenPosition=edit?null:position;viewConfig=saved;
        const ch=position?.entry_target?.channel;
        if(ch)viewConfig={...saved,entry_policy:{mode:'channel'},channel_policy:ch.policy,close_policy:ch.close_policy||{}};
        else if(position)viewConfig={...saved,entry_policy:{mode:'fixed'},close_policy:{}};
        if(!edit)fill(saved.channel_policy);
        if(ch)fill(ch.policy);
        else if(!edit&&saved.entry_policy?.mode!=='channel')fill({...defaults,...saved.channel_policy,window_seconds:saved.entry_policy?.target_window_seconds||604800});
        tab=position?'history':'rule';
        get('strategy-preview-title').textContent=edit?'设置通道策略':position?'本单冻结策略示意':'已保存策略示意';
        document.querySelector('.strategy-preview-controls').hidden=readOnly;
        get('channel-settings-save').hidden=!edit;
        get('channel-settings-form').querySelectorAll('input,select').forEach(node=>{node.disabled=!edit;});
        get('strategy-preview-error').textContent='';preview=null;dialog.showModal();draw();refresh();
    }
    function close(){if(!saving)get('strategy-preview-dialog')?.close();}
    window.StrategyChannelUI={load,read,validate,open,basis:()=>({mode:saved.entry_policy?.mode||'fixed',policy:{...defaults,...saved.channel_policy}})};
    document.addEventListener('DOMContentLoaded',()=>{
        const dialog=get('strategy-preview-dialog');if(!dialog)return;fill(defaults);
        const help=document.createElement('div');help.id='channel-field-help';
        help.className='field-help-tooltip';help.setAttribute('role','tooltip');help.hidden=true;
        dialog.appendChild(help);
        let helpOwner=null;
        const hideHelp=()=>{
            if(helpOwner){helpOwner.setAttribute('aria-expanded','false');helpOwner.removeAttribute('aria-describedby');}
            helpOwner=null;help.hidden=true;
        };
        const showHelp=button=>{
            hideHelp();
            const rect=button.getBoundingClientRect(),bounds=dialog.getBoundingClientRect();
            const leftEdge=Math.max(8,bounds.left+8),rightEdge=Math.min(window.innerWidth-8,bounds.right-8);
            const topEdge=Math.max(8,bounds.top+8),bottomEdge=Math.min(window.innerHeight-8,bounds.bottom-8);
            if(rect.bottom<topEdge||rect.top>bottomEdge||rightEdge-leftEdge<40)return;
            help.textContent=button.dataset.tooltip;help.style.width=`${Math.min(280,rightEdge-leftEdge)}px`;
            help.style.maxHeight=`${bottomEdge-topEdge}px`;help.style.visibility='hidden';help.hidden=false;
            const size=help.getBoundingClientRect();
            const left=Math.max(leftEdge,Math.min(rect.left,rightEdge-size.width));
            const preferred=rect.bottom+7+size.height<=bottomEdge?rect.bottom+7:rect.top-7-size.height;
            help.style.left=`${left}px`;help.style.top=`${Math.max(topEdge,Math.min(preferred,bottomEdge-size.height))}px`;
            help.style.visibility='visible';helpOwner=button;
            button.setAttribute('aria-expanded','true');button.setAttribute('aria-describedby',help.id);
        };
        dialog.querySelectorAll('.field-help-icon').forEach(button=>{
            button.addEventListener('mouseenter',()=>showHelp(button));
            button.addEventListener('mouseleave',hideHelp);
            button.addEventListener('focus',()=>{if(button.matches(':focus-visible'))showHelp(button);});
            button.addEventListener('blur',hideHelp);
            button.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();});
            button.addEventListener('keydown',event=>{
                if(event.key==='Escape'){event.preventDefault();event.stopPropagation();hideHelp();}
            });
        });
        dialog.addEventListener('scroll',hideHelp,true);dialog.addEventListener('close',hideHelp);
        window.addEventListener('resize',hideHelp);
        // A running panel may retain the prior template until its next restart.
        document.querySelectorAll('.strategy-preview-toolbar,.strategy-preview-tabs').forEach(node=>node.remove());
        get('channel-settings-open').addEventListener('click',()=>open(true));get('strategy-preview-open').addEventListener('click',()=>open(false));
        get('strategy-preview-close').addEventListener('click',close);
        dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
        dialog.addEventListener('close',()=>{controller?.abort();requestId++;clearTimeout(timer);fill(beforeEdit);frozenPosition=null;lastFocus?.focus();});
        get('channel-settings-form').addEventListener('submit',event=>event.preventDefault());
        get('channel-settings-form').addEventListener('input',schedule);get('channel-settings-form').addEventListener('change',schedule);
        get('channel-settings-save').addEventListener('click',async()=>{
            if(saving)return;
            if(!get('channel-settings-form').reportValidity())return;
            const error=validate()||(get('position-mode')?.value==='spread_grid'?'请先将持仓模式改为单开；通道仍可同时持有不同品种。':'');
            if(error){get('strategy-preview-error').textContent=error;return;}
            const previous=get('execution-entry-mode').value;get('execution-entry-mode').value='channel';
            get('execution-entry-mode').dispatchEvent(new Event('change',{bubbles:true}));
            saving=true;get('channel-settings-save').disabled=true;get('strategy-preview-close').disabled=true;
            get('channel-settings-form').querySelectorAll('input,select').forEach(node=>{node.disabled=true;});
            try{const result=await window.saveStrategyConfig();if(result?.success){beforeEdit={...saved.channel_policy};saving=false;close();}
                else{get('execution-entry-mode').value=previous;get('execution-entry-mode').dispatchEvent(new Event('change',{bubbles:true}));get('strategy-preview-error').textContent=get('config-result')?.textContent||'保存失败';}}
            finally{saving=false;get('channel-settings-save').disabled=false;get('strategy-preview-close').disabled=false;
                get('channel-settings-form').querySelectorAll('input,select').forEach(node=>{node.disabled=readOnly;});}
        });
        window.addEventListener('resize',()=>chart?.resize());
        if(window.ResizeObserver)new ResizeObserver(()=>chart?.resize()).observe(get('strategy-preview-chart'));
    });
})();
