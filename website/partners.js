/* Partner content lives in partners.json; see README.md for editing instructions. */
(() => {
  const section = document.getElementById('partners');
  const list = document.getElementById('partner-list');
  const logoDirectory = new URL('assets/partners/', document.baseURI);

  function safeUrl(value, isLogo = false) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const url = new URL(value, document.baseURI);
      if (!['https:', 'http:'].includes(url.protocol)) return null;
      if (isLogo && (url.origin !== logoDirectory.origin || !url.pathname.startsWith(logoDirectory.pathname))) return null;
      return url.href;
    } catch {
      return null;
    }
  }

  function createPartner(partner) {
    if (!partner || typeof partner.name !== 'string' || !partner.name.trim()) return null;

    const name = partner.name.trim();
    const website = safeUrl(partner.url);
    const logo = safeUrl(partner.logo, true);
    const showName = partner.showName === true || !logo;
    const item = document.createElement('li');
    const card = document.createElement(website ? 'a' : 'div');
    card.className = 'partner-card';
    if (website) card.href = website;

    const label = document.createElement('span');
    label.className = 'partner-name';
    label.textContent = name;
    label.hidden = !showName;

    if (logo) {
      const image = document.createElement('img');
      // Avoid announcing the name twice when the visible label is enabled.
      image.alt = showName ? '' : name;
      image.width = 152;
      image.height = 44;
      image.addEventListener('error', () => {
        image.remove();
        label.hidden = false;
      }, { once: true });
      image.src = logo;
      card.append(image);
    }

    card.append(label);
    item.append(card);
    return item;
  }

  async function loadPartners() {
    if (!section || !list) return;
    try {
      const response = await fetch(new URL('partners.json', document.baseURI));
      if (!response.ok) throw new Error(`Partner list returned HTTP ${response.status}`);
      const partners = await response.json();
      if (!Array.isArray(partners)) throw new Error('Partner list must be a JSON array');

      const items = partners.map(createPartner).filter(Boolean);
      list.replaceChildren(...items);
      section.hidden = items.length === 0;
    } catch (error) {
      // A missing or invalid list must not interrupt the rest of the page.
      console.warn('Unable to load project partners:', error);
    }
  }

  loadPartners();
})();
