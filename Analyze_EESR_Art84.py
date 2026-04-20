import os
import re
import pandas as pd
import time
import glob
import google.generativeai as genai
import fitz  # PyMuPDF
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 0. API & 환경 설정
# ==========================================
# Gemini API Key (제공해주신 키 연동 완료)
GEMINI_API_KEY = "발급받은_GEMINI_API_KEY_여기에_붙여넣기"

genai.configure(api_key=GEMINI_API_KEY)
# 모델 선택 (텍스트 분류/분석에 뛰어난 1.5-flash-latest 사용)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

def setup_chrome_driver(download_dir):
    """undetected_chromedriver를 활용하여 봇 탐지 우회"""
    options = uc.ChromeOptions()
    # 봇 탐지 우회를 위해 헤드리스 모드는 쓰지 않는 것을 권장
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--start-maximized")
    
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True  # Chrome 내장 뷰어 대신 바로 다운로드
    }
    options.add_experimental_option("prefs", prefs)
    
    # undetected 크롬 구동
    driver = uc.Chrome(options=options)
    return driver

def wait_for_download(download_dir, timeout=30):
    """다운로드가 완료될 때까지 크롬 임시 확장자(.crdownload)가 없어지길 대기"""
    seconds = 0
    while seconds < timeout:
        time.sleep(1)
        if any(filename.endswith(".crdownload") for filename in os.listdir(download_dir)):
            seconds += 1
        else:
            return True
    return False

def get_latest_pdf(download_dir):
    """가장 최근에 다운로드된 PDF 파일 경로 반환"""
    list_of_files = glob.glob(os.path.join(download_dir, '*.pdf'))
    if not list_of_files:
        return None
    latest_file = max(list_of_files, key=os.path.getmtime)
    return latest_file

def download_eesr_pdf_selenium(driver, app_number, download_dir):
    """
    Selenium을 사용하여 EPO Register에서 EESR PDF를 다운로드합니다.
    """
    print(f"  -> [{app_number}] Selenium 브라우저 우회로 EPO Register 검색 중...")
    
    # 번호 처리 (EP prefix가 없으면 붙여서 검색 안전성을 높임)
    search_num = str(app_number).replace(" ", "").upper()
    if not search_num.startswith("EP"):
        search_num = "EP" + search_num

    # 1. 문서 검색 페이지 접속
    url = f"https://register.epo.org/application?number={search_num}"
    driver.get(url)
    
    try:
        # 'All documents' 탭이 보일 때까지 대기 후 클릭
        wait = WebDriverWait(driver, 10)
        docs_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(@href, 'tab=doclist')]")))
        docs_tab.click()
        
        # 2. 문서 리스트 화면에서 'Search Report' 텍스트 찾기
        time.sleep(3) # 테이블 로딩 대기
        
        # 테이블 내 텍스트 매칭
        doc_links = driver.find_elements(By.XPATH, "//td[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'search report')]/following-sibling::td//a[contains(@href, 'document')] | //td[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'search report')]/preceding-sibling::td//a[contains(@href, 'document')]")
        
        if not doc_links:
            # 경우에 따라 텍스트가 다를 수 있으므로 폭넓은 XPATH 보조 탐색
            all_links = driver.find_elements(By.XPATH, "//table[@id='doclist']//tr")
            found = False
            for row in all_links:
                if "search report" in row.text.lower():
                    pdf_btn = row.find_element(By.XPATH, ".//a[contains(@href, 'document')]")
                    pdf_btn.click()
                    found = True
                    break
            
            if not found:
                print("  -> 결과 페이지 내 Search Report 링크를 찾지 못했습니다.")
                return None
        else:
            # 첫번째 (보통 가장 최신 또는 전체 EESR인 것) 클릭
            doc_links[0].click()
            
        print("  -> PDF 다운로드 버튼 클릭 완료. 파일 저장 대기 중...")
        
        # 다운로드 완료 대기
        wait_for_download(download_dir)
        
        # 가장 최근 생성된 파일명 확인 후 식별하기 쉽게 Rename
        time.sleep(1.5)
        latest_pdf = get_latest_pdf(download_dir)
        
        if latest_pdf:
            new_name = os.path.join(download_dir, f"{app_number}_EESR.pdf")
            # 이미 있으면 덮어쓰기 위해 삭제
            if os.path.exists(new_name):
                os.remove(new_name)
            os.rename(latest_pdf, new_name)
            return new_name
        else:
            print("  -> 다운로드 폴더에서 PDF를 획득 조작 실패.")
            return None
            
    except Exception as e:
        print(f"  -> 브라우저 검색 실패 (문서 없음 또는 로딩 지연): {e}")
        return None

def extract_pdf_text_and_check_art84(pdf_path):
    """PDF에서 텍스트를 추출하고 Article 84 언급 여부를 다각적 정규표현식으로 확인"""
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text("text") + "\n"
        doc.close()
        
        # 확장된 Article 84 정규식 향상 (Art. 84, Article 84, Art84, A. 84, A.84 등 포괄)
        pattern = r"\b(?:Article|Art\.?|A\.?)\s*84\b"
        if re.search(pattern, full_text, re.IGNORECASE):
            return True, full_text
        else:
            return False, full_text
            
    except Exception as e:
        print(f"[ERROR] PDF 파싱 에러({pdf_path}): {e}")
        return False, ""

def classify_art84_with_gemini(text):
    """Gemini API를 사용하여 분류 작업 수행"""
    prompt = f"""
    당신은 유럽 특허법(EPC)을 다루는 전문 특허 번역가이자 변리사입니다.
    아래는 Extended European Search Report (EESR) 문서의 일부 텍스트입니다.
    해당 문서들에는 'Article 84' (또는 A.84, Art. 84) 거절 사유가 명시되어 있습니다.
    
    주어진 텍스트를 분석하여, 거절 사유를 다음 4가지 종류 중 **정확히 하나**로만 분류해주세요.
    응답은 반드시 "종류 X" 포맷으로 시작하고, 그 뒤에 짧고 명확한 근거(1~2문장)를 한국어로 작성하세요.

    [분류 기준]
    종류 1. 명확성 부족 (Lack of Clarity): "about", "substantially", "suitable" 등 모호하거나 불확실한 상대적 용어 사용, 선택적 특징 표기, 또는 핵심적 특징(Essential features) 결여.
    종류 2. 명세서에 의한 뒷받침 부족 (Lack of Support): 청구항과 명세서 내용 불일치(Inconsistency), 발명 범위 초과 등 일치성 위반.
    종류 3. 간결성 및 청구항 수 (Conciseness & Number of Claims): 독립항 과다(Rule 62a) 또는 청구항 중복으로 권리 범위 파악 곤란 (Rule 29(5) 위반 동반 등).
    종류 4. 기타: 위 3가지에 명확히 해당하지 않는 기타 유형.

    [문서 텍스트]
    {text[:8000]}  # 텍스트 앞부분 위주로 전송 (근거 문장 파악용)
    """

    try:
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[ERROR] Gemini API 오류: {e}")
        return "분류 에러"

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
    excel_path = os.path.join(desktop_path, "특허검색_EESR검색.xlsx")
    output_excel_path = os.path.join(desktop_path, "특허검색_EESR검색_분류결과_Selenium.xlsx")
    pdf_save_dir = os.path.join(base_dir, "EESR_Downloads_Selenium")
    
    os.makedirs(pdf_save_dir, exist_ok=True)
    
    if not os.path.exists(excel_path):
        print(f"[ERROR] 바탕화면에 '{os.path.basename(excel_path)}' 파일이 없습니다!")
        print("[INFO] [테스트 모드]: 샘플 Dataframe을 생성하여 진행합니다.")
        df = pd.DataFrame({'출원번호': ['20123456', '20987654']})
    else:
        print("엑셀 파일을 불러오는 중...")
        df = pd.read_excel(excel_path)
    
    # 엑셀 열에 출원번호가 있는지 체크
    col_app = None
    for col in df.columns:
        if "출원" in str(col) or "Application" in str(col):
            col_app = col
            break
            
    if not col_app:
        print("[ERROR] '출원번호' 관련 열을 찾을 수 없습니다. (열 이름 확인 필요)")
        return

    if 'Article84_여부' not in df.columns:
        df['Article84_여부'] = "조사안됨"
    if '거절사유_유형분류' not in df.columns:
        df['거절사유_유형분류'] = ""

    print("\n[크롬 드라이버 초기화 중... 최초 1회 빈 창이 뜰 수 있습니다]")
    driver = setup_chrome_driver(pdf_save_dir)

    try:
        for idx, row in df.iterrows():
            app_number = str(row[col_app]).strip()
            if not app_number or app_number == 'nan':
                continue
                
            print(f"\n[Run] [{idx+1}/{len(df)}] 타겟 출원번호: {app_number}")
            
            # 1. Selenium으로 EESR PDF 다운로드
            pdf_file = download_eesr_pdf_selenium(driver, app_number, pdf_save_dir)
            
            fallback_pdf = os.path.join(pdf_save_dir, f"{app_number}_EESR.pdf")
            if not pdf_file and os.path.exists(fallback_pdf):
                pdf_file = fallback_pdf
                
            if not pdf_file:
                print("  -> EESR PDF 문서를 다운로드/찾을 수 없어 Skip 합니다.")
                df.at[idx, 'Article84_여부'] = "PDF없음"
                continue
                
            # 2. PDF 파싱 및 Article 84 체크
            is_art84, extract_txt = extract_pdf_text_and_check_art84(pdf_file)
            
            if is_art84:
                print("  -> [HIT] Article 84 (A.84 등) 관련 거절 발견! Gemini 분류 시작...")
                df.at[idx, 'Article84_여부'] = "존재(Yes)"
                
                # 3. Gemini로 분류 (API 속도 제한 고려 delay)
                time.sleep(2)
                class_result = classify_art84_with_gemini(extract_txt)
                print(f"  -> {class_result[:60]}...")
                df.at[idx, '거절사유_유형분류'] = class_result
                
            else:
                print("  -> 기술된 텍스트 중 Article 84 거절 조항이 없어 패스합니다.")
                df.at[idx, 'Article84_여부'] = "없음(No)"
                df.at[idx, '거절사유_유형분류'] = "Skip"
                
    finally:
        driver.quit()
        
    # 최종 결과 저장
    try:
        df.to_excel(output_excel_path, index=False)
        print(f"\n[OK] 완료! 결과 파일이 저장되었습니다: {output_excel_path}")
    except Exception as e:
        print(f"[ERROR] 엑셀 저장 실패: {e}")

if __name__ == "__main__":
    main()
