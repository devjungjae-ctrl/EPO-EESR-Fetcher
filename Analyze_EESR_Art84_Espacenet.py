import os
import re
import glob
import time
import pandas as pd
import fitz  # PyMuPDF
import google.generativeai as genai
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 0. API & 환경 설정
# ==========================================
# Gemini API Key
GEMINI_API_KEY = "발급받은_GEMINI_API_KEY_여기에_붙여넣기"
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "EESR_Downloads_Espacenet")

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# ==========================================
# 1. Selenium 봇 설정 (에스파스넷 전용)
# ==========================================
def setup_espacenet_driver(download_dir):
    """undetected_chromedriver를 활용하여 에스파스넷 Cloudflare 우회"""
    options = uc.ChromeOptions()
    # 봇 탐지 우회를 위해 헤드리스 모드는 쓰지 않는 것을 권장
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--start-maximized")
    
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)
    
    driver = uc.Chrome(options=options)
    return driver

def wait_for_download(download_dir, timeout=30):
    seconds = 0
    while seconds < timeout:
        time.sleep(1)
        if any(filename.endswith(".crdownload") for filename in os.listdir(download_dir)):
            seconds += 1
        else:
            return True
    return False

def get_latest_pdf(download_dir):
    list_of_files = glob.glob(os.path.join(download_dir, '*.pdf'))
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getmtime)

# ==========================================
# 2. 에스파스넷 접근 및 EESR 확보
# ==========================================
def download_from_espacenet(driver, app_number, download_dir):
    """
    Espacenet에 접속하여 Original document PDF를 다운로드합니다.
    """
    print(f"  -> [{app_number}] 에스파스넷 서버 우회 접근 중...")
    
    # 1. 통합 검색창 쿼리로 바로 찌르기
    url = f"https://worldwide.espacenet.com/patent/search?q={app_number}"
    driver.get(url)
    wait = WebDriverWait(driver, 15)
    
    try:
        # 쿠키 허용 팝업이 있다면 무시 또는 클릭 (Timeout 3초로 짧게)
        try:
            cookie_btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button.cookie-consent__button--accept, button.cc-btn.cc-allow, button#agree, button[id*='agree']"))
            )
            cookie_btn.click()
            time.sleep(1)
        except:
            pass # 쿠키 팝업이 안보이면 패스
        
        # 2. 에스파스넷은 출원번호만 넣어도 해당 문헌으로 자동 리다이렉트 되거나 단일 결과창이 뜸
        # 'Original document' 탭 접근 시도
        try:
            # 탭이 보일 때까지 대기
            orig_doc_tab = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "ep-tab-item[data-tab-id='originalDocument'], li[data-tab-value='originalDocument']")
            ))
            orig_doc_tab.click()
            print("  -> 'Original document' 탭 진입 완료")
        except:
            # 혹여 리스트 화면이라면 맨 처음 결과를 클릭
            try:
                first_result = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.publication-number, .result-item-link, .patent-result-item a.title")))
                first_result.click()
                time.sleep(2)
                orig_doc_tab = wait.until(EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "ep-tab-item[data-tab-id='originalDocument'], li[data-tab-value='originalDocument']")
                ))
                orig_doc_tab.click()
                print("  -> 단일 문헌 접속 후 'Original document' 탭 진입 완료")
            except Exception as e:
                print(f"  -> Original document 탭을 찾지 못했습니다: {str(e)[:50]}...")
                return None
        
        # 3. PDF 다운로드 아이콘(버튼) 찾기 및 클릭
        time.sleep(3) # 원문 렌더링 대기
        try:
            download_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-cy='download-button'], a.action-menu__item--download, .icon-download")))
            download_btn.click()
            print("  -> 원문(PDF) 다운로드 버튼 클릭!")
        except Exception as e:
            print(f"  -> PDF 다운로드 버튼을 찾지 못했습니다: {str(e)[:50]}...")
            return None
            
        wait_for_download(download_dir)
        time.sleep(1.5)
        
        # 4. 파일 이름 추적 및 변경
        latest_pdf = get_latest_pdf(download_dir)
        if latest_pdf:
            new_name = os.path.join(download_dir, f"{app_number}_Espacenet_A1_EESR.pdf")
            if os.path.exists(new_name): os.remove(new_name)
            os.rename(latest_pdf, new_name)
            return new_name
        else:
            print("  -> 타임아웃: 폴더에 PDF가 들어오지 않았습니다.")
            return None

    except Exception as e:
        print(f"  -> 에스파스넷 크롤링 중 오류 발생: {str(e)[:50]}...")
        return None

# ==========================================
# 3. NLP 판독기 (기존과 동일)
# ==========================================
def extract_pdf_and_check_art84(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        # EESR(Search Report)은 주로 뒤쪽 페이지에 첨부되므로 뒤부터 스캔하거나 전체 스캔
        for page in doc:
            full_text += page.get_text("text") + "\n"
        doc.close()
        
        pattern = r"\b(?:Article|Art\.?|A\.?)\s*84\b"
        if re.search(pattern, full_text, re.IGNORECASE):
            return True, full_text
        return False, full_text
    except Exception as e:
        print(f"  -> PDF 파싱 에러: {e}")
        return False, ""

def analyze_with_gemini(text):
    prompt = f"""
    아래는 유럽 특허청(EPO)의 Search Report 문서 추출 텍스트입니다.
    Article 84에 기반한 거절 사유가 있는지 판별하여, 반드시 다음 4가지 중 정확히 하나로만 분류해주세요.
    
    1. 명확성 부족 (Lack of Clarity)
    2. 명세서에 의한 뒷받침 부족 (Lack of Support)
    3. 간결성 및 청구항 수 (Conciseness & Number of Claims)
    4. 기타
    
    [텍스트 추출본 (요약)]
    {text[-8000:]} # EESR은 통상 문서 뒷부분에 위치하므로 뒷부분 위주로 전송
    """
    try:
        res = gemini_model.generate_content(prompt)
        text_result = res.text.strip()
        if "명확성 부족" in text_result or "Lack of Clarity" in text_result: return "명확성 부족"
        elif "뒷받침 부족" in text_result or "Lack of Support" in text_result: return "뒷받침 부족"
        elif "간결성" in text_result or "Conciseness" in text_result: return "간결성"
        elif "기타" in text_result or "Other" in text_result: return "기타"
        else: return "Article 84 없음"
    except Exception as e:
        return f"분류 에러 ({e})"

# ==========================================
# 4. 메인 파이프라인 구동
# ==========================================
def main():
    print("==================================================")
    print(" Espacenet (에스파스넷) EESR 수집 봇 초기화 중... ")
    print("==================================================")
    
    excel_path = os.path.join(BASE_DIR, '..', '특허검색_EESR검색.xlsx')
    if not os.path.exists(excel_path):
        df = pd.DataFrame({'출원번호': ['18817854.5']})
    else:
        df = pd.read_excel(excel_path)
        
    if '거절사유_분류(Espacenet)' not in df.columns:
        df['거절사유_분류(Espacenet)'] = ""
        
    print("\n[우회 접속용 봇 브라우저를 구동합니다...]")
    driver = setup_espacenet_driver(DOWNLOAD_DIR)
    
    try:
        for idx, row in df.iterrows():
            raw_app_num = str(row['출원번호']).strip()
            if not raw_app_num or raw_app_num.lower() == 'nan': continue
            
            # 소수점 부식 방어막: .5 등 제거
            app_num = raw_app_num.split('.')[0]
            print(f"\n[진행률: {idx+1}/{len(df)}] 문헌 탐색: {app_num}")
            
            # Espacenet 다운로드 체인
            pdf_path = download_from_espacenet(driver, app_num, DOWNLOAD_DIR)
            
            if not pdf_path:
                # 이미 디렉토리에 폴백 경로가 있는지 확인
                fallback_pdf = os.path.join(DOWNLOAD_DIR, f"{app_num}_Espacenet_A1_EESR.pdf")
                if os.path.exists(fallback_pdf):
                    pdf_path = fallback_pdf
                else:
                    df.at[idx, '거절사유_분류(Espacenet)'] = "Skip (PDF 확보 실패)"
                    continue
            
            # NLP 판독 체인
            is_art84, txt = extract_pdf_and_check_art84(pdf_path)
            if is_art84:
                print("  -> [HIT] Article 84 거절 이력 분석 중...")
                time.sleep(2)
                cls_result = analyze_with_gemini(txt)
                print(f"  -> 결과: {cls_result}")
                df.at[idx, '거절사유_분류(Espacenet)'] = cls_result
            else:
                print("  -> [PASS] Article 84 거절 사유 미발견")
                df.at[idx, '거절사유_분류(Espacenet)'] = "거절이력 없음"
                
    finally:
        driver.quit()
        
    result_path = os.path.join(BASE_DIR, '특허검색_EESR검색_Espacenet_결과.xlsx')
    df.to_excel(result_path, index=False)
    print(f"\n[성공] 에스파스넷 추출 작업이 완료되어 다음 경로에 저장되었습니다: {result_path}")

if __name__ == "__main__":
    main()
