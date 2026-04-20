import os
import re
import glob
import time
import pandas as pd
import fitz  # PyMuPDF
import google.generativeai as genai
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 0. 환경 설정
# ==========================================
# Google Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "발급받은_GEMINI_API_KEY_여기에_붙여넣기")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "EESR_Downloads_TIPIS")

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# ==========================================
# 1. TIPIS 인증용 브라우저 세팅 
# ==========================================
def setup_tipis_driver(download_dir):
    """
    사내망 보안 인증 유지를 위해 기존 크롬 사용자 데이터를 연동하거나
    수동 로그인을 유도할 수 있도록 세팅합니다.
    """
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-dev-shm-usage")
    # 사내 인증 우회 및 탭 충돌 방지를 위해 sandbox 해제
    options.add_argument("--no-sandbox")
    
    # 사내 망에서 PDF 다운로드를 Viewer로 열지 않고 로컬에 즉시 받게 함
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)
    
    driver = uc.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver

def wait_for_download_complete(download_dir, before_count, timeout=30):
    """
    크롬이 .crdownload 확장자를 없애고 온전한 pdf를 다운받을 때까지 모니터링합니다.
    """
    seconds = 0
    while seconds < timeout:
        time.sleep(1)
        current_pdfs = glob.glob(os.path.join(download_dir, '*.pdf'))
        crdownloads = glob.glob(os.path.join(download_dir, '*.crdownload'))
        if len(current_pdfs) > before_count and len(crdownloads) == 0:
            return True
        seconds += 1
    return False

def get_latest_pdf(download_dir):
    list_of_files = glob.glob(os.path.join(download_dir, '*.pdf'))
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getmtime)

# ==========================================
# 2. TIPIS 자동화 핵심 시퀀스
# ==========================================
def scrap_eesr_from_tipis(driver, lg_ref, download_dir):
    """
    메인 창에서 LG REF 검색 -> 새 창 이동 -> 업무이력 탭 클릭 -> EESR PDF 다운로드
    """
    try:
        if not lg_ref or str(lg_ref).strip() == 'nan':
            return None
            
        lg_ref = str(lg_ref).strip()
        print(f"\n  -> [LG REF: {lg_ref}] TIPIS 조회 시작...")
        
        # 1. 봇이 다른 창에 있다면 언제나 메인대시보드(인덱스 0)로 돌아옴
        driver.switch_to.window(driver.window_handles[0])
        main_url = "https://tipis.lge.com/"
        if driver.current_url != main_url and not driver.current_url.startswith(main_url):
            driver.get(main_url)
            
        wait = WebDriverWait(driver, 15)
        
        # 2. 우상단 Search 창 찾기 및 검색 (엔터)
        try:
            # Placeholder가 Search인 input을 찾음 (사용자 가이드 기준)
            search_box = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@placeholder='Search'] | //input[contains(@title, 'Search')]")))
            search_box.clear()
            search_box.send_keys(lg_ref)
            time.sleep(0.5)
            search_box.send_keys(Keys.RETURN)
        except Exception as e:
            print(f"  -> [오류] 메인 화면에서 Search 창을 찾을 수 없습니다: {str(e)[:40]}")
            return None
            
        # 3. 새로운 창(팝업 뷰어)이 뜰 때까지 대기
        time.sleep(3) # 창이 파생될 시간 확보
        if len(driver.window_handles) > 1:
            # 팝업 창(마지막 핸들)으로 포커스 전환
            driver.switch_to.window(driver.window_handles[-1])
            print("  -> 새 창(상세 페이지) 전환 완료.")
        else:
            print("  -> [경고] 검색 결과에 따른 새 창이 열리지 않았습니다(REF 오류 또는 검색결과 없음).")
            return None
            
        # 4. "업무이력" 탭 클릭
        try:
            # "업무이력" 글씨가 포함된 탭 버튼 클릭
            history_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[normalize-space(text())='업무이력'] | //*[contains(text(), '업무이력')]")))
            history_tab.click()
            print("  -> '업무이력' 탭 접근 완료. 문건 스캔 중...")
            time.sleep(2)
        except Exception as e:
            print("  -> [오류] '업무이력' 탭을 찾지 못했습니다.")
            driver.close()
            return None

        # 5. 리스트 내 지정된 이름(OA접수 보고 > Search Report 등) 탐색 및 다운로드
        before_download_count = len(glob.glob(os.path.join(download_dir, '*.pdf')))
        pdf_downloaded_path = None
        
        try:
            # 조건: 'Search Report' 혹은 'Erweiterter Europaeischer Recherchebericht'가 포함된 행(Row) 탐색
            # 보통 테이블 <tr> 내부의 <a>, <span>, <td> 구조일 확률이 큼
            target_str_1 = "Search Report"
            target_str_2 = "Erweiterter Europaeischer Recherchebericht"
            
            # 텍스트가 포함된 모든 a, td 요소 추적
            items = driver.find_elements(By.XPATH, f"//*[contains(text(), '{target_str_1}') or contains(text(), '{target_str_2}')]")
            
            if len(items) > 0:
                print("  -> [HIT] 해당 EESR/Search Report 원본 문건 발견! 다운로드 격발...")
                # 가장 먼저 찾아진 아이템의 영역을 클릭 (경우에 따라 아이콘이나 a태그 등일 수 있음)
                # 직접 클릭이 안될 경우 대비 JS 클릭
                driver.execute_script("arguments[0].click();", items[0])
                
                # 크롬 엔진이 로컬에 pdf를 떨굴 때까지 대기
                if wait_for_download_complete(download_dir, before_download_count):
                    time.sleep(1)
                    latest_pdf = get_latest_pdf(download_dir)
                    if latest_pdf:
                        new_name = os.path.join(download_dir, f"{lg_ref}_SearchReport.pdf")
                        if os.path.exists(new_name):
                            try: os.remove(new_name)
                            except: pass
                        os.rename(latest_pdf, new_name)
                        pdf_downloaded_path = new_name
            else:
                print("  -> [PASS] 업무이력 목록에 지정된 EESR 서류가 없습니다.")
                
        except Exception as e:
            print(f"  -> [오류] 문서 다운로드 중 에러 발생: {str(e)[:40]}")
            
        # 작업 완료 후 팝업창 닫기 및 메인창 원복
        driver.close()
        driver.switch_to.window(driver.window_handles[0])
        
        return pdf_downloaded_path
        
    except Exception as e:
        print(f"  -> 비정상 오류 발생: {str(e)[:40]}")
        try:
            # 꼬인 상황 복구: 새창이 켜져있다면 모두 닫고 0번 창 유지
            while len(driver.window_handles) > 1:
                driver.switch_to.window(driver.window_handles[-1])
                driver.close()
            driver.switch_to.window(driver.window_handles[0])
        except:
            pass
        return None

# ==========================================
# 3. 텍스트 추출 및 심층 NLP 분석
# ==========================================
def extract_pdf_and_check_art84(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text("text") + "\n"
        doc.close()
        
        # Art 84, Article 84, A.84 
        pattern = r"\b(?:Article|Art\.?|A\.?)\s*84\b"
        if re.search(pattern, full_text, re.IGNORECASE):
            return True, full_text
        return False, full_text
    except Exception as e:
        print(f"  -> PDF 디코딩 실패: {e}")
        return False, ""

def analyze_with_gemini(text):
    prompt = f"""
    아래는 EPO 심사관이 작성한 Search Report 또는 ESOP 문헌의 일부입니다.
    이 의견서에서 'Article 84'를 적용하여 지적/거절한 사유가 무엇인지 파악하고, 
    반드시 다음 4가지 카테고리 중 **하나로만** 답하세요. (응답 예시: 1. 명확성 부족)
    
    분류 기준:
    1. 명확성 부족 (Lack of Clarity)
    2. 명세서에 의한 뒷받침 부족 (Lack of Support)
    3. 간결성 및 청구항 수 (Conciseness & Number of Claims)
    4. 기타
    
    [문헌 텍스트 (앞 8000글자)]
    {text[:8000]}
    """
    try:
        res = gemini_model.generate_content(prompt)
        text_result = res.text.strip()
        if "명확성 부족" in text_result or "Lack of Clarity" in text_result: return "명확성 부족"
        elif "뒷받침 부족" in text_result or "Lack of Support" in text_result: return "뒷받침 부족"
        elif "간결성" in text_result or "Conciseness" in text_result: return "간결성"
        elif "기타" in text_result or "Other" in text_result: return "기타"
        else: return "조항 84 기반 지적 모호"
    except Exception as e:
        return f"분석 오류: {e}"

# ==========================================
# 4. Main 파이프라인
# ==========================================
def main():
    print("================================================================")
    print(" LGE TIPIS 자동화 시스템 가동 (Search Report / ESOP 다운로더) ")
    print("================================================================")
    
    excel_path = os.path.join(BASE_DIR, '..', '특허검색_EESR검색.xlsx')
    
    if not os.path.exists(excel_path):
        print(f"[오류] 데이터소스를 찾을 수 없습니다. (경로: {excel_path})")
        return
        
    df = pd.read_excel(excel_path)
    
    if 'LG REF' not in df.columns:
        print("[오류] 엑셀 파일 내에 'LG REF' 컬럼이 존재하지 않습니다.")
        return
        
    if 'Article84_여부' not in df.columns:    df['Article84_여부'] = ""
    if '거절사유(ESOP)' not in df.columns:  df['거절사유(ESOP)'] = ""
        
    print("\n[TIPIS 사내망 봇 전용 브라우저를 구동합니다...]")
    driver = setup_tipis_driver(DOWNLOAD_DIR)
    
    try:
        # 최초 1회 로그인 수동 대기 (SSO 방어선)
        driver.get("https://tipis.lge.com/")
        print("-----------------------------------------------------------------")
        print("[핵심] 브라우저 창이 열렸습니다. 사내 SSO 로그인을 직접 진행해 주세요!")
        print("      로그인이 완전히 끝난 후, 메인 화면이 나타나면")
        print("      이 터미널 창으로 돌아와서 [Enter] 키를 누르세요.")
        print("-----------------------------------------------------------------")
        
        input(">>> 로그인을 성공적으로 마치셨다면 터미널 창에서 [Enter] 키를 누르세요...")
        
        print("\n[수동 인증 승인] 업무 스크래핑을 개시합니다!\n")

        for idx, row in df.iterrows():
            lg_ref = str(row['LG REF']).strip()
            
            # REF 번호를 기준으로 스크래핑 동작 가동
            pdf_path = scrap_eesr_from_tipis(driver, lg_ref, DOWNLOAD_DIR)
            
            if not pdf_path:
                df.at[idx, 'Article84_여부'] = "수집 실패/문서없음"
                df.at[idx, '거절사유(ESOP)'] = "-"
                continue
                
            # 심사관 텍스트 파싱 및 탐지
            is_art84, txt = extract_pdf_and_check_art84(pdf_path)
            if is_art84:
                df.at[idx, 'Article84_여부'] = "제기됨"
                print("  -> [주의] 심사관이 Article 84를 지적했습니다! AI 판독 중...")
                time.sleep(2)
                cls_result = analyze_with_gemini(txt)
                print(f"  -> 심사관 지적 사유: {cls_result}")
                df.at[idx, '거절사유(ESOP)'] = cls_result
            else:
                print("  -> [클린] 해당 문서에는 Article 84 지적 내역이 없습니다.")
                df.at[idx, 'Article84_여부'] = "없음"
                df.at[idx, '거절사유(ESOP)'] = "-"
                
    finally:
        driver.quit()
        
    result_path = os.path.join(BASE_DIR, '특허검색_TIPIS통합_결과.xlsx')
    df.to_excel(result_path, index=False)
    print(f"\n[작업 완료] 사내망 시스템 연동 EESR 분석이 완료되었습니다: {result_path}")

if __name__ == "__main__":
    main()
