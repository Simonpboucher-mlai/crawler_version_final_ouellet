import requests
from bs4 import BeautifulSoup
import os
from urllib.parse import urljoin, urlparse
import logging
import time
from collections import defaultdict, deque
import re
from datetime import datetime
import hashlib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import html2text

# Désactiver les avertissements SSL si nécessaire
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


class WebCrawler:
    def __init__(self, start_url, max_depth=2):
        self.start_url = start_url
        self.max_depth = max_depth
        self.visited_pages = set()
        self.downloaded_files = set()
        self.domain = urlparse(start_url).netloc

        # Extraction du pattern de langue depuis l'URL de départ
        self.language_path = re.search(r'/(fr|en)-(ca|us)/', start_url)
        if self.language_path:
            self.language_pattern = self.language_path.group(0)
            self.language_code = self.language_path.group(1)
            self.country_code = self.language_path.group(2)
        else:
            self.language_pattern = None
            self.language_code = None
            self.country_code = None

        # Liste des segments d'URL à exclure
        self.excluded_paths = ['selecteur-de-produits']

        # Création des dossiers nécessaires avec timestamp
        self.base_dir = f"crawler_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.create_directories()

        # Configuration du logging
        self.setup_logging()

        # Statistiques
        self.stats = defaultdict(int)

        # Liste des extensions à télécharger
        self.downloadable_extensions = {
            'PDF': ['.pdf'],
            'Image': ['.png', '.jpg', '.jpeg', '.gif', '.svg'],
            'Doc': ['.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']
        }

        # Extraire toutes les extensions téléchargeables en une seule liste
        self.all_downloadable_exts = set(ext for exts in self.downloadable_extensions.values() for ext in exts)

        # Mapping des Content-Type aux extensions pour chaque type de fichier
        self.content_type_mapping = {
            'PDF': {
                'application/pdf': '.pdf'
            },
            'Image': {
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/gif': '.gif',
                'image/svg+xml': '.svg',
                # Ajouter d'autres Content-Type d'images si nécessaire
            },
            'Doc': {
                'application/msword': '.doc',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
                'application/vnd.ms-excel': '.xls',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
                'application/vnd.ms-powerpoint': '.ppt',
                'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
                # Ajouter d'autres Content-Type de documents si nécessaire
            }
        }

        # Configuration de la session avec gestion des retries
        self.session = self.setup_session()

        # Configuration du convertisseur HTML vers Markdown
        self.html_converter = html2text.HTML2Text()
        self.html_converter.ignore_links = False
        self.html_converter.body_width = 0
        self.html_converter.ignore_images = True

    def setup_session(self):
        """Configure une session requests avec retry et timeouts"""
        session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.verify = False  # Pour désactiver la vérification SSL si nécessaire
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/91.0.4472.124 Safari/537.36'
        })
        return session

    def create_directories(self):
        """Crée la structure de dossiers nécessaire pour le crawler"""
        directories = ['content', 'PDF', 'Image', 'Doc', 'logs']
        for dir_name in directories:
            path = os.path.join(self.base_dir, dir_name)
            os.makedirs(path, exist_ok=True)

    def setup_logging(self):
        """Configure le système de logging"""
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                logging.FileHandler(os.path.join(self.base_dir, 'logs', 'crawler.log'), encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        logging.info(f"Starting crawler with language pattern: {self.language_pattern}")

    def should_exclude(self, url):
        """Détermine si une URL doit être exclue en fonction des segments définis"""
        for excluded in self.excluded_paths:
            if excluded in url:
                return True
        return False

    def is_same_language(self, url):
        """Vérifie si l'URL respecte le même pattern linguistique que l'URL de départ"""
        if not self.language_pattern:
            return True
        return self.language_pattern in url

    def is_downloadable_file(self, url):
        """Vérifie si l'URL pointe vers un fichier téléchargeable en utilisant des expressions régulières."""
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()
        # Créer un pattern regex pour détecter les extensions, même avec des suffixes comme .pdf.aspx
        pattern = re.compile(r'\.(' + '|'.join([ext.strip('.') for exts in self.downloadable_extensions.values() for ext in exts]) + r')(\.[a-z0-9]+)?$', re.IGNORECASE)
        return bool(pattern.search(path))

    def get_file_type_and_extension(self, url, response):
        """
        Détermine le type de fichier et l'extension appropriée en fonction de l'URL et du Content-Type.
        Retourne un tuple (file_type, extension).
        """
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()

        # Première tentative : déduire le type de fichier basé sur l'URL
        for file_type, extensions in self.downloadable_extensions.items():
            for ext in extensions:
                # Adapter le pattern pour inclure des suffixes comme .aspx
                pattern = re.compile(re.escape(ext) + r'(\.[a-z0-9]+)?$', re.IGNORECASE)
                if pattern.search(path):
                    return file_type, self.content_type_mapping[file_type].get(response.headers.get('Content-Type', '').lower(), ext)

        # Seconde tentative : déduire le type de fichier basé sur le Content-Type
        content_type = response.headers.get('Content-Type', '').lower()
        for file_type, mapping in self.content_type_mapping.items():
            if content_type in mapping:
                return file_type, mapping[content_type]

        # Si aucun type n'est déterminé, retourner None
        return None, None

    def sanitize_filename(self, url, file_type, extension):
        """Crée un nom de fichier sécurisé à partir de l'URL, en ajustant l'extension si nécessaire."""
        # Création d'un hash court de l'URL
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]

        # Extraction du dernier segment de l'URL
        filename = url.split('/')[-1]
        if not filename:
            filename = 'index'

        # Nettoyage du nom de fichier
        filename = re.sub(r'[^\w\-_.]', '_', filename)

        # Suppression des extensions existantes
        name, _ = os.path.splitext(filename)

        # Définir l'extension en fonction du type de fichier
        if not extension:
            extension = '.txt'  # Extension par défaut si non déterminée

        return f"{name}_{url_hash}{extension}"

    def download_file(self, url, file_type):
        """Télécharge un fichier et le sauvegarde dans le dossier approprié."""
        try:
            logging.info(f"Attempting to download {file_type} file from: {url}")
            response = self.session.get(url, stream=True, timeout=20)

            if response.status_code == 200:
                # Déterminer le type de fichier et l'extension
                file_type_detected, extension = self.get_file_type_and_extension(url, response)
                if not file_type_detected:
                    logging.warning(f"Could not determine the file type for: {url}")
                    return False

                # Renommer le fichier correctement
                filename = self.sanitize_filename(url, file_type_detected, extension)
                save_path = os.path.join(self.base_dir, file_type_detected, filename)

                # Sauvegarder le fichier
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                self.stats[f'{file_type_detected}_downloaded'] += 1
                self.downloaded_files.add(url)
                logging.info(f"Successfully downloaded {file_type_detected}: {filename}")
                return True

            else:
                logging.warning(f"Failed to download {file_type} from {url}: Status code {response.status_code}")
                return False

        except Exception as e:
            logging.error(f"Error downloading {url}: {str(e)}")
            return False

    def convert_links_to_absolute(self, soup, base_url):
        """Convertit tous les liens relatifs en liens absolus."""
        # Inclure les balises <embed>, <iframe>, et <object> en plus des <a> et <link>
        for tag in soup.find_all(['a', 'embed', 'iframe', 'object'], href=True):
            href = tag.get('href') or tag.get('src')
            if href:
                absolute_url = urljoin(base_url, href)
                if tag.name in ['embed', 'iframe', 'object']:
                    tag['src'] = absolute_url
                else:
                    tag['href'] = absolute_url
        return soup

    def clean_text(self, text):
        """Nettoie et formate le texte en réduisant les espaces inutiles."""
        if not text:
            return ""

        # Suppression des caractères spéciaux inutiles
        text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)

        # Normalisation des espaces
        text = re.sub(r'\s+', ' ', text)

        # Suppression des espaces autour des sauts de ligne
        text = re.sub(r'\s*\n\s*', '\n', text)

        return text.strip()

    def extract_content(self, url):
        """Extrait le contenu d'une page et le sauvegarde en format markdown."""
        logging.info(f"Extracting content from: {url}")

        try:
            if self.is_downloadable_file(url):
                logging.info(f"Skipping content extraction for downloadable file: {url}")
                return

            response = self.session.get(url, timeout=20)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')

                # Suppression des éléments non désirés
                for element in soup.find_all(['nav', 'header', 'footer', 'script', 'style', 'aside', 'iframe']):
                    element.decompose()

                # Extraction du contenu principal
                main_content = (
                    soup.find('main') or 
                    soup.find('article') or 
                    soup.find('div', class_='content') or
                    soup.find('div', id='content')
                )

                if main_content:
                    # Conversion des liens relatifs en liens absolus
                    main_content = self.convert_links_to_absolute(main_content, url)

                    # Conversion du contenu principal en markdown
                    markdown_content = self.html_converter.handle(str(main_content))

                    # Construction du contenu final avec titre
                    content_parts = []

                    # Ajout du titre
                    title = soup.find('h1')
                    if title:
                        content_parts.append(f"# {title.get_text().strip()}")

                    # Ajout de l'URL source
                    content_parts.append(f"**Source:** {url}")

                    # Ajout du contenu principal
                    content_parts.append(markdown_content)

                    # Nettoyage du contenu
                    content = self.clean_text('\n\n'.join(content_parts))

                    if content:
                        # Création du nom de fichier
                        filename = self.sanitize_filename(url, 'Doc', '.txt')  # Utiliser 'Doc' avec extension '.txt' pour les pages
                        save_path = os.path.join(self.base_dir, 'content', filename)
                        with open(save_path, 'w', encoding='utf-8') as f:
                            f.write(content)

                        self.stats['pages_processed'] += 1
                        logging.info(f"Successfully saved content to: {filename}")
                    else:
                        logging.warning(f"No significant content found for: {url}")

                    # Traitement des fichiers téléchargeables trouvés dans la page
                    for tag in main_content.find_all(['a', 'embed', 'iframe', 'object'], href=True):
                        href = tag.get('href') or tag.get('src')
                        if href:
                            file_url = urljoin(url, href)
                            if self.is_downloadable_file(file_url) and file_url not in self.downloaded_files:
                                file_type_detected, _ = self.get_file_type_and_extension(file_url, self.session.head(file_url, allow_redirects=True))
                                if not file_type_detected:
                                    # Fallback to determining file type via GET request
                                    response_head = self.session.head(file_url, allow_redirects=True)
                                    file_type_detected, _ = self.get_file_type_and_extension(file_url, response_head)
                                if file_type_detected:
                                    self.download_file(file_url, file_type_detected)

                else:
                    logging.warning(f"No main content found for: {url}")

        except Exception as e:
            logging.error(f"Error processing {url}: {str(e)}")

    def extract_urls(self, start_url):
        """Extrait récursivement les URLs d'une page jusqu'à la profondeur maximale."""
        queue = deque()
        queue.append((start_url, 0))
        self.visited_pages.add(start_url)

        while queue:
            current_url, depth = queue.popleft()

            if depth > self.max_depth:
                continue

            # Vérifier si l'URL doit être exclue
            if self.should_exclude(current_url):
                logging.info(f"Excluded URL due to matching excluded paths: {current_url}")
                continue

            logging.info(f"Extracting URLs from: {current_url} (depth: {depth})")

            try:
                if self.is_downloadable_file(current_url):
                    file_type = self.get_file_type_and_extension(current_url, self.session.head(current_url, allow_redirects=True))[0]
                    if file_type:
                        self.download_file(current_url, file_type)
                    continue

                response = self.session.get(current_url, timeout=20)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')

                    # Recherche de fichiers téléchargeables et de liens
                    for tag in soup.find_all(['a', 'link', 'embed', 'iframe', 'object'], href=True):
                        href = tag.get('href') or tag.get('src')
                        if href:
                            absolute_url = urljoin(current_url, href)
                            parsed_url = urlparse(absolute_url)

                            # Vérification si c'est un fichier téléchargeable
                            if self.is_downloadable_file(absolute_url):
                                file_type_detected, _ = self.get_file_type_and_extension(absolute_url, self.session.head(absolute_url, allow_redirects=True))
                                if not file_type_detected:
                                    # Fallback to determining file type via GET request
                                    response_head = self.session.head(absolute_url, allow_redirects=True)
                                    file_type_detected, _ = self.get_file_type_and_extension(absolute_url, response_head)
                                if file_type_detected and absolute_url not in self.downloaded_files:
                                    self.download_file(absolute_url, file_type_detected)
                                    self.downloaded_files.add(absolute_url)
                                continue

                            # Vérification des liens internes
                            if (self.domain in parsed_url.netloc and 
                                self.is_same_language(absolute_url) and
                                absolute_url not in self.visited_pages and
                                not absolute_url.endswith(('#', 'javascript:void(0)', 'javascript:;')) and
                                not self.should_exclude(absolute_url)):

                                # Ajouter à la queue avec profondeur incrémentée
                                queue.append((absolute_url, depth + 1))
                                self.visited_pages.add(absolute_url)

            except Exception as e:
                logging.error(f"Error crawling {current_url}: {str(e)}")

    def crawl(self):
        """Méthode principale de crawling."""
        start_time = time.time()
        logging.info(f"Starting crawl of {self.start_url}")
        logging.info(f"Language pattern: {self.language_pattern}")
        logging.info(f"Maximum depth: {self.max_depth}")

        try:
            # Phase 1: Extraction des URLs
            logging.info("Phase 1: Starting URL extraction")
            self.extract_urls(self.start_url)

            # Phase 2: Extraction du contenu
            logging.info("Phase 2: Starting content extraction")
            for i, url in enumerate(self.visited_pages, 1):
                if self.is_downloadable_file(url):
                    continue  # Les fichiers ont déjà été téléchargés
                logging.info(f"Processing URL {i}/{len(self.visited_pages)}: {url}")
                self.extract_content(url)

            logging.info("Phase 2: Completed content extraction")

            # Génération du rapport final
            end_time = time.time()
            self.generate_report(end_time - start_time)

        except Exception as e:
            logging.error(f"Critical error during crawling: {str(e)}")
            self.generate_report(time.time() - start_time, error=str(e))

    def generate_report(self, duration, error=None):
        """Génère un rapport détaillé du processus de crawling."""
        report_sections = []

        # En-tête du rapport
        report_sections.append(f"""
Crawler Report
==============
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Configuration
------------
Start URL: {self.start_url}
Language Pattern: {self.language_pattern}
Max Depth: {self.max_depth}
Duration: {duration:.2f} seconds

Statistics
---------
Total URLs found: {len(self.visited_pages)}
Pages processed: {self.stats['pages_processed']}
Files downloaded:
- PDFs: {self.stats['PDF_downloaded']}
- Images: {self.stats['Image_downloaded']}
- Documents: {self.stats['Doc_downloaded']}
""")

        # Section des erreurs si présentes
        if error:
            report_sections.append(f"""
Errors
------
Critical Error: {error}
""")

        # Liste des URLs crawlées
        report_sections.append("""
Processed URLs
-------------
""")
        for url in sorted(self.visited_pages):
            report_sections.append(url)

        # Liste des fichiers générés
        report_sections.append("""
Generated Files
--------------
""")

        for directory in ['content', 'PDF', 'Image', 'Doc']:
            dir_path = os.path.join(self.base_dir, directory)
            if os.path.exists(dir_path):
                files = os.listdir(dir_path)
                report_sections.append(f"\n{directory} Files ({len(files)}):")
                for file in sorted(files):
                    report_sections.append(f"- {file}")

        # Sauvegarde du rapport
        report_content = '\n'.join(report_sections)
        report_path = os.path.join(self.base_dir, 'crawler_report.txt')

        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report_content)
            logging.info(f"Report generated successfully: {report_path}")
        except Exception as e:
            logging.error(f"Error generating report: {str(e)}")

        # Création d'un fichier de résumé
        summary = f"""
Crawling Summary
---------------
Start URL: {self.start_url}
Total URLs: {len(self.visited_pages)}
Pages Processed: {self.stats['pages_processed']}
Total Files Downloaded: {sum(self.stats[k] for k in ['PDF_downloaded', 'Image_downloaded', 'Doc_downloaded'])}
Duration: {duration:.2f} seconds
Status: {'Completed with errors' if error else 'Completed successfully'}
"""
        try:
            with open(os.path.join(self.base_dir, 'summary.txt'), 'w', encoding='utf-8') as f:
                f.write(summary)
            logging.info(f"Summary generated successfully: {os.path.join(self.base_dir, 'summary.txt')}")
        except Exception as e:
            logging.error(f"Error generating summary: {str(e)}")


def main():
    """Fonction principale pour exécuter le crawler."""
    # Configuration initiale
    start_url = "https://www.ouellet.com/fr-ca/"  # Remplacez par l'URL de départ souhaitée
    max_depth = 1  # Ajustez la profondeur selon vos besoins

    try:
        # Création et exécution du crawler
        crawler = WebCrawler(start_url, max_depth)
        crawler.crawl()

    except Exception as e:
        logging.error(f"Fatal error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
