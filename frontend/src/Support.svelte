<script>
  let { open = false } = $props();

  const addresses = [
    { net: 'Sol', value: '5a9QbRNeWzZPp3kjJGQbiY6GXaBvMopueRR89n5DPfBL' },
    { net: 'TON', value: 'UQDk7ldJ2s61XURDl7oG3-v0p1VEcPh8sn5M3c2lBX2gpID_' },
    { net: 'TRON', value: 'TTMMdFgENLywtLSZj9H2FnUUF79yLHF9cE' },
    { net: 'USDT Sol', value: '5a9QbRNeWzZPp3kjJGQbiY6GXaBvMopueRR89n5DPfBL' },
  ];

  async function copy(el) {
    try {
      await navigator.clipboard.writeText(el.textContent);
      const prev = el.textContent;
      el.textContent = 'copied';
      setTimeout(() => (el.textContent = prev), 900);
    } catch {
      /* clipboard unavailable */
    }
  }
</script>

<div id="support" class:open>
  <div class="panel">
    <div class="head">For supporting the underpants</div>
    {#each addresses as a}
      <div class="addr">
        <span class="net">{a.net}</span>
        <code
          role="button"
          tabindex="0"
          onclick={(e) => copy(e.currentTarget)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              copy(e.currentTarget);
            }
          }}
        >{a.value}</code>
      </div>
    {/each}
  </div>
  <button type="button" onclick={() => (open = !open)}>Support</button>
</div>
