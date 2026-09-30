import { SharedArray } from 'k6/data';

// Realistic User Agents covering Chrome/Firefox/Safari/Edge on Windows/Mac/Linux/iOS/Android
export const userAgents = new SharedArray('user-agents', () => [
  // Chrome on Windows
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',

  // Chrome on macOS
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',

  // Chrome on Linux
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',

  // Firefox on Windows
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0',

  // Firefox on macOS
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 13.5; rv:120.0) Gecko/20100101 Firefox/120.0',

  // Firefox on Linux
  'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
  'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0',

  // Safari on macOS
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15',

  // Safari on iOS
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1',
  'Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1',
  'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',

  // Edge on Windows
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
  'Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0',

  // Edge on macOS
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',

  // Chrome on Android
  'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
  'Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
  'Mozilla/5.0 (Linux; Android 13; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',

  // Chrome on iOS
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1',

  // Samsung Internet
  'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36',

  // Firefox on Android
  'Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0',
]);

// Bot User Agents (separate export for bot-simulation scenarios)
export const botUserAgents = new SharedArray('bot-user-agents', () => [
  'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
  'Mozilla/5.0 (compatible; Bingbot/2.0; +http://www.bing.com/bingbot.htm)',
  'DuckDuckBot/1.0; (+http://duckduckgo.com/duckduckbot.html)',
  'Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)',
  'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
]);

// IP addresses mapped to real countries and cities
export const ipLocations = new SharedArray('ip-locations', () => [
  // United States
  { ip: '8.8.8.8', country: 'United States', city: 'Mountain View' },
  { ip: '1.1.1.1', country: 'United States', city: 'Los Angeles' },
  { ip: '4.4.4.4', country: 'United States', city: 'New York' },
  { ip: '208.67.222.222', country: 'United States', city: 'San Francisco' },
  { ip: '12.34.56.78', country: 'United States', city: 'Chicago' },
  { ip: '13.82.0.0', country: 'United States', city: 'Seattle' },
  { ip: '20.20.20.20', country: 'United States', city: 'Austin' },
  { ip: '74.125.0.0', country: 'United States', city: 'Boston' },

  // United Kingdom
  { ip: '81.2.69.142', country: 'United Kingdom', city: 'London' },
  { ip: '81.2.69.143', country: 'United Kingdom', city: 'Manchester' },
  { ip: '82.163.0.0', country: 'United Kingdom', city: 'Birmingham' },
  { ip: '86.0.0.0', country: 'United Kingdom', city: 'Edinburgh' },

  // Germany
  { ip: '46.4.0.0', country: 'Germany', city: 'Berlin' },
  { ip: '85.214.0.0', country: 'Germany', city: 'Munich' },
  { ip: '91.0.0.0', country: 'Germany', city: 'Frankfurt' },
  { ip: '80.187.0.0', country: 'Germany', city: 'Hamburg' },

  // France
  { ip: '2.0.0.0', country: 'France', city: 'Paris' },
  { ip: '5.39.0.0', country: 'France', city: 'Lyon' },

  // Canada
  { ip: '24.48.0.0', country: 'Canada', city: 'Toronto' },
  { ip: '99.224.0.0', country: 'Canada', city: 'Vancouver' },

  // India
  { ip: '103.21.244.0', country: 'India', city: 'Mumbai' },
  { ip: '49.206.0.0', country: 'India', city: 'Bangalore' },

  // Australia
  { ip: '1.128.0.0', country: 'Australia', city: 'Sydney' },

  // Japan
  { ip: '126.0.0.0', country: 'Japan', city: 'Tokyo' },

  // Brazil
  { ip: '177.0.0.0', country: 'Brazil', city: 'S\u00e3o Paulo' },

  // Singapore
  { ip: '103.9.0.0', country: 'Singapore', city: 'Singapore' },
  { ip: '182.255.0.0', country: 'Singapore', city: 'Singapore' },

  // Netherlands
  { ip: '31.13.24.0', country: 'Netherlands', city: 'Amsterdam' },
  { ip: '77.172.0.0', country: 'Netherlands', city: 'Rotterdam' },

  // Spain
  { ip: '83.32.0.0', country: 'Spain', city: 'Madrid' },
  { ip: '88.0.0.0', country: 'Spain', city: 'Barcelona' },
  { ip: '84.88.0.0', country: 'Spain', city: 'Valencia' },
  { ip: '95.16.0.0', country: 'Spain', city: 'Seville' },

  // South Korea
  { ip: '1.201.0.0', country: 'South Korea', city: 'Seoul' },
  { ip: '58.120.0.0', country: 'South Korea', city: 'Busan' },

  // Italy
  { ip: '79.0.0.0', country: 'Italy', city: 'Rome' },
  { ip: '93.32.0.0', country: 'Italy', city: 'Milan' },

  // Sweden
  { ip: '78.64.0.0', country: 'Sweden', city: 'Stockholm' },
  { ip: '85.24.0.0', country: 'Sweden', city: 'Gothenburg' },

  // Poland
  { ip: '5.173.0.0', country: 'Poland', city: 'Warsaw' },
  { ip: '83.0.0.0', country: 'Poland', city: 'Krak\u00f3w' },

  // Russia
  { ip: '91.192.0.0', country: 'Russia', city: 'Moscow' },
  { ip: '178.176.0.0', country: 'Russia', city: 'Saint Petersburg' },

  // Turkey
  { ip: '78.160.0.0', country: 'Turkey', city: 'Istanbul' },
  { ip: '88.224.0.0', country: 'Turkey', city: 'Ankara' },

  // Indonesia
  { ip: '36.64.0.0', country: 'Indonesia', city: 'Jakarta' },
  { ip: '103.10.0.0', country: 'Indonesia', city: 'Surabaya' },

  // Thailand
  { ip: '58.8.0.0', country: 'Thailand', city: 'Bangkok' },
  { ip: '103.22.0.0', country: 'Thailand', city: 'Chiang Mai' },

  // Malaysia
  { ip: '60.48.0.0', country: 'Malaysia', city: 'Kuala Lumpur' },
  { ip: '103.18.0.0', country: 'Malaysia', city: 'Penang' },

  // Philippines
  { ip: '49.144.0.0', country: 'Philippines', city: 'Manila' },
  { ip: '112.198.0.0', country: 'Philippines', city: 'Cebu' },

  // Vietnam
  { ip: '113.160.0.0', country: 'Vietnam', city: 'Ho Chi Minh City' },
  { ip: '14.160.0.0', country: 'Vietnam', city: 'Hanoi' },

  // Egypt
  { ip: '62.68.0.0', country: 'Egypt', city: 'Cairo' },
  { ip: '197.32.0.0', country: 'Egypt', city: 'Alexandria' },

  // UAE
  { ip: '5.62.0.0', country: 'United Arab Emirates', city: 'Dubai' },
  { ip: '31.193.0.0', country: 'United Arab Emirates', city: 'Abu Dhabi' },

  // Saudi Arabia
  { ip: '46.28.0.0', country: 'Saudi Arabia', city: 'Riyadh' },
  { ip: '109.224.0.0', country: 'Saudi Arabia', city: 'Jeddah' },

  // Portugal
  { ip: '85.138.0.0', country: 'Portugal', city: 'Lisbon' },
  { ip: '188.81.0.0', country: 'Portugal', city: 'Porto' },

  // Belgium
  { ip: '78.20.0.0', country: 'Belgium', city: 'Brussels' },
  { ip: '85.255.0.0', country: 'Belgium', city: 'Antwerp' },

  // Austria
  { ip: '77.116.0.0', country: 'Austria', city: 'Vienna' },
  { ip: '88.116.0.0', country: 'Austria', city: 'Graz' },

  // Switzerland
  { ip: '77.56.0.0', country: 'Switzerland', city: 'Zurich' },
  { ip: '85.0.0.0', country: 'Switzerland', city: 'Geneva' },

  // Norway
  { ip: '84.208.0.0', country: 'Norway', city: 'Oslo' },
  { ip: '91.184.0.0', country: 'Norway', city: 'Bergen' },

  // Denmark
  { ip: '80.62.0.0', country: 'Denmark', city: 'Copenhagen' },
  { ip: '87.48.0.0', country: 'Denmark', city: 'Aarhus' },

  // Finland
  { ip: '62.78.0.0', country: 'Finland', city: 'Helsinki' },
  { ip: '88.112.0.0', country: 'Finland', city: 'Espoo' },

  // Ireland
  { ip: '87.32.0.0', country: 'Ireland', city: 'Dublin' },
  { ip: '46.22.0.0', country: 'Ireland', city: 'Cork' },

  // New Zealand
  { ip: '49.50.0.0', country: 'New Zealand', city: 'Auckland' },
  { ip: '210.48.0.0', country: 'New Zealand', city: 'Wellington' },

  // Czech Republic
  { ip: '78.128.0.0', country: 'Czech Republic', city: 'Prague' },
  { ip: '89.102.0.0', country: 'Czech Republic', city: 'Brno' },
]);

// Realistic referrer URLs
export const referrers = new SharedArray('referrers', () => [
  'https://www.google.com/',
  'https://www.google.co.uk/',
  'https://www.bing.com/',
  'https://duckduckgo.com/',
  'https://www.reddit.com/',
  'https://twitter.com/',
  'https://www.facebook.com/',
  'https://www.linkedin.com/',
  'https://www.youtube.com/',
  'https://news.ycombinator.com/',
  'https://www.producthunt.com/',
  'https://github.com/',
  'https://t.co/',
  'https://www.pinterest.com/',
  'https://www.instagram.com/',
]);
