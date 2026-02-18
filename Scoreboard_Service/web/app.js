async function poll() {
  try {
    const r = await fetch('/state', { cache: 'no-store' });
    const s = await r.json();

    document.getElementById('clock').textContent = s.clock || '--:--';
    document.getElementById('period').textContent = `PER ${s.period_display || '-'}`;
    document.getElementById('running').textContent = s.clock_running ? 'running' : 'stopped';
    document.getElementById('horn').textContent = `horn: ${s.horn ? 'ON' : 'off'}`;

    document.getElementById('homeName').textContent = s.home.name || 'HOME';
    document.getElementById('awayName').textContent = s.away.name || 'AWAY';
    document.getElementById('homeScore').textContent = s.home.score ?? 0;
    document.getElementById('awayScore').textContent = s.away.score ?? 0;

    document.getElementById('homePens').textContent = (s.home.penalties || []).join(' ') || '----';
    document.getElementById('awayPens').textContent = (s.away.penalties || []).join(' ') || '----';

    document.getElementById('sumTop').textContent = s.summary.top || '-';
    document.getElementById('sumBottom').textContent = s.summary.bottom || '-';
    document.getElementById('sumMain').textContent = s.summary.main || '-';

    document.getElementById('meta').textContent =
      `sport=${s.sport ?? '-'}  pnu=${s.period_number ?? '-'}  intermission=${s.in_intermission}`;

  } catch (e) {
    console.error(e);
  } finally {
    setTimeout(poll, 250);
  }
}
poll();
